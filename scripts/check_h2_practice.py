#!/usr/bin/env python3
"""Проверить два заполненных Excel-бланка пробной разметки без изменения реестров."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKET = Path("manifests/results/h2_practice_20261011_draft_v1")
DEFAULT_TEMPLATES = Path("outputs/h2_practice_20261011_excel_v1")
MAX_PRINTED_ISSUES = 10

# Прямой запуск из scripts/ требует добавить корень до импортов локальных модулей.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.h2_practice import (  # noqa: E402
    prepare_h2_practice,
    publish_h2_practice,
)


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Описать исходный пакет, отдельные ответы и новый каталог результатов."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packet-dir", type=Path, default=project_root / DEFAULT_PACKET,
        help="Исходный пакет с packet.json и inputs.jsonl",
    )
    parser.add_argument(
        "--template-dir", type=Path, default=project_root / DEFAULT_TEMPLATES,
        help="Каталог исходных пустых Excel-бланков с verification.json",
    )
    parser.add_argument(
        "--author-file", type=Path, required=True,
        help="Отдельная заполненная копия Excel-бланка автора проекта",
    )
    parser.add_argument(
        "--supervisor-file", type=Path, required=True,
        help="Отдельная заполненная копия Excel-бланка научного руководителя",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Новый каталог внутри manifests/results; перезапись запрещена",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Проверить бланки и рассчитать сравнение без записи файлов",
    )

    return parser


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить локальные ответы и отдельно сообщить ошибки и разногласия."""

    root = Path(project_root).resolve()
    parser = _build_parser(root)
    parsed = parser.parse_args(arguments)

    try:
        prepared = prepare_h2_practice(
            project_root=root,
            packet_dir=parsed.packet_dir,
            template_dir=parsed.template_dir,
            author_file=parsed.author_file,
            supervisor_file=parsed.supervisor_file,
            output_dir=parsed.output_dir,
        )
        created = publish_h2_practice(prepared, dry_run=parsed.dry_run)

    except (ValueError, OSError) as exception:
        print(f"ОШИБКА: не удалось проверить бланки H2: {exception}", file=sys.stderr)
        return 1

    counts = prepared.report["counts"]

    if parsed.dry_run:
        status = "Предварительная проверка без записи"

    elif created:
        status = "Создан пакет проверки"

    else:
        status = "Запись не выполнена"

    print(f"{status}. Карточек: {counts['items']}.")
    print(
        f"Ошибок: {counts['errors']}; полных пар ответов: {counts['complete_pairs']}; "
        f"разногласий: {counts['disagreements']}."
    )

    issues = prepared.report["issues"]

    for issue in issues[:MAX_PRINTED_ISSUES]:
        print(
            f"[{issue['role']}] {issue['sheet']}!{issue['cell']}: "
            f"{issue['message']} ({issue['code']})"
        )

    if len(issues) > MAX_PRINTED_ISSUES:
        print(f"Остальных замечаний: {len(issues) - MAX_PRINTED_ISSUES}.")

    if counts["errors"]:
        print("Ответы нельзя экспортировать: сначала исправьте ошибки заполнения.")

    print(f"{'Будущий каталог' if parsed.dry_run else 'Результат'}: {prepared.output_dir}")
    print("Разногласия не являются ошибками; итоговые метки H2 не назначены.")
    print("Независимость разметки и утверждение памятки автоматически не подтверждаются.")
    print("Исходные бланки, пакет и реестры не изменены; скачивания не выполнялись.")

    return 2 if counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
