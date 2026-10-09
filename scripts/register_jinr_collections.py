#!/usr/bin/env python3
"""Проверить и явно зарегистрировать полные материалы сборников ОИЯИ без сети."""

from __future__ import annotations

import sys

from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Команда должна запускаться напрямую из scripts/ без установки пакета.
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.register_jinr_articles import _build_parser, _commit_plan
from src.corpus.jinr_collection_input import read_collection_batch
from src.corpus.jinr_collection_registration import (
    plan_collection_batch,
    reconcile_collection_plan,
)
from src.corpus.manifests import ManifestError, ManifestStore
from src.corpus.registration import RegistrationOptions


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить весь пакет и записать его только при явном параметре --commit."""

    root = project_root.resolve()
    parser = _build_parser(
        root, description=__doc__,
        input_help="Один JSON-пакет проверенных полнотекстовых материалов научных сборников",
    )
    arguments_parsed = parser.parse_args(arguments)

    if not arguments_parsed.check_files_only and (
        not arguments_parsed.rights_record_id
        or not arguments_parsed.metadata_rights_record_id
    ):
        parser.error(
            "Для планирования нужны --rights-record-id и --metadata-rights-record-id; "
            "для проверки файлов используйте --check-files-only"
        )

    try:
        path = arguments_parsed.input.resolve()
        batch = read_collection_batch(root, path)
        pdf_paths = {
            pdf_path
            for article in batch.entries
            for pdf_path in (article.pdf_path, *article.entry["pdf"]["aliases"])
        }
        print(f"Проверены материалы сборников: {len(batch.entries)}; путей PDF: {len(pdf_paths)}.")
        print("Статус допуска остаётся pending; допуск к обучению и H2 не назначается.")

        if arguments_parsed.check_files_only:
            print("Происхождение проверено; реестры и допуск по правам не изменялись.")

            return 0

        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="bulk",
            rights_record_ids=tuple(arguments_parsed.rights_record_id),
            extraction_method="not_started",
            extraction_version="jinr-collection-pdf-v1",
            response_representation="pdf",
            request_context_type="work",
        )
        plan = plan_collection_batch(
            root, path, options,
            collected_at=datetime.now(timezone.utc).isoformat(),
            metadata_rights_record_ids=tuple(arguments_parsed.metadata_rights_record_id),
        )

        for warning in plan.warnings:
            print(f"Предупреждение: {warning}")

        store = ManifestStore(
            project_root=root,
            manifest_dir=arguments_parsed.manifest_dir,
            schema_dir=root / "manifests/schemas",
        )
        result = _commit_plan(
            store, plan, dry_run=not arguments_parsed.commit,
            reconcile=reconcile_collection_plan,
        )
        action = "Предварительная проверка" if result.dry_run else "Регистрация"
        print(f"{action} завершена.")
        print(f"Добавлено: {result.inserted}; обновлено: {result.updated}.")

        return 0

    except (ValueError, OSError, ManifestError) as exception:
        parser.error(f"Не удалось обработать пакет материалов сборников: {exception}")


if __name__ == "__main__":
    raise SystemExit(main())
