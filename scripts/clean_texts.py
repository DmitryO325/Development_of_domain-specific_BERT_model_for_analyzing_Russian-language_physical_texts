#!/usr/bin/env python3
"""Создать отдельную версию очищенных текстов по явному локальному плану."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Команда запускается напрямую без установки проекта как пакета.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.manifests import ManifestError  # noqa: E402
from src.preprocess.cleaning_io import prepare_cleaning, publish_cleaning  # noqa: E402


def main(
    arguments: list[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    schema_dir: Path | None = None,
) -> int:
    """Проверить исходники и записать кандидатов, не меняя реестры корпуса."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="JSON с закреплёнными страницами и правилами")
    parser.add_argument("--dry-run", action="store_true", help="Проверить без записи файлов")
    args = parser.parse_args(arguments)

    try:
        prepared = prepare_cleaning(
            args.plan, project_root=project_root, schema_dir=schema_dir
        )
        created = publish_cleaning(prepared, dry_run=args.dry_run)

    except (ValueError, OSError, ManifestError) as exception:
        print(f"ОШИБКА: {exception}", file=sys.stderr)
        return 1

    totals = prepared.report["totals"]

    if args.dry_run:
        status = "Предварительная проверка"

    elif created:
        status = "Создан новый пакет"

    else:
        status = "Точный повтор, запись не нужна"

    print(
        f"{status}. Документов: {totals['documents']}; "
        f"страниц: {totals['pages_before']} → {totals['pages_after']}."
    )
    print(f"Изменений: {totals['changes']}; замечаний для проверки: {totals['issues']}.")
    print(f"{'Будущий отчёт' if args.dry_run else 'Отчёт'}: {prepared.output_dir / 'report.json'}")
    print(
        "Это кандидаты для проверки: качество и допуск к обучению не утверждены. "
        "Исходники и реестры не изменены."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
