#!/usr/bin/env python3
"""Проверить и явно зарегистрировать отдельный пакет тезисов ОИЯИ без сети."""

from __future__ import annotations

import sys

from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Команда должна запускаться напрямую из scripts/ без установки пакета.
sys.path.insert(0, str(PROJECT_ROOT))

# Импорты идут после настройки пути для прямого запуска без установки пакета.
from scripts.register_jinr_articles import _build_parser, _commit_plan  # noqa: E402
from src.corpus.jinr_abstract_input import read_abstract_batch  # noqa: E402
from src.corpus.jinr_abstract_registration import plan_abstract_batch, reconcile_abstract_plan  # noqa: E402
from src.corpus.manifests import ManifestError, ManifestStore  # noqa: E402
from src.corpus.registration import RegistrationOptions  # noqa: E402


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить весь пакет и записать его только при явном параметре --commit."""

    root = project_root.resolve()
    parser = _build_parser(
        root, description=__doc__,
        input_help="Один JSON-пакет проверенных тезисов с отдельным согласием на жанр",
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
        batch = read_abstract_batch(root, path)
        pdf_paths = {
            pdf_path
            for article in batch.entries
            for pdf_path in (article.pdf_path, *article.entry["pdf"]["aliases"])
        }
        print(f"Проверены тезисы: {len(batch.entries)}; путей PDF: {len(pdf_paths)}.")
        print("Статус допуска остаётся pending; допуск к обучению и H2 не назначается.")

        if arguments_parsed.check_files_only:
            print("Происхождение и согласие проверены; реестры и допуск по правам не изменялись.")

            return 0

        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="bulk",
            rights_record_ids=tuple(arguments_parsed.rights_record_id),
            extraction_method="not_started",
            extraction_version="jinr-abstract-pdf-v1",
            response_representation="pdf",
            request_context_type="work",
        )
        plan = plan_abstract_batch(
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
            reconcile=reconcile_abstract_plan,
        )
        action = "Предварительная проверка" if result.dry_run else "Регистрация"
        print(f"{action} завершена.")
        print(f"Добавлено: {result.inserted}; обновлено: {result.updated}.")

        return 0

    except (ValueError, OSError, ManifestError) as exception:
        parser.error(f"Не удалось обработать пакет тезисов: {exception}")


if __name__ == "__main__":
    raise SystemExit(main())
