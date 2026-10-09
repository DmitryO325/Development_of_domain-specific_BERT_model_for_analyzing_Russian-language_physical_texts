#!/usr/bin/env python3
"""Проверить пакет журнальных статей ОИЯИ и явно зарегистрировать его без сети."""

from __future__ import annotations

import argparse
import sys

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Команда должна запускаться напрямую из scripts/ без установки пакета.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.jinr_article_input import ArticleBatch, read_article_batch
from src.corpus.jinr_article_registration import (
    plan_article_batch,
    reconcile_article_plan,
)
from src.corpus.manifests import (
    CommitResult,
    ManifestConcurrencyError,
    ManifestError,
    ManifestPlan,
    ManifestStore,
)
from src.corpus.registration import RegistrationOptions

MAX_COMMIT_ATTEMPTS = 3


def _build_parser(
    project_root: Path,
    *,
    description: str | None = __doc__,
    input_help: str = "Один JSON-пакет подготовленных журнальных статей ОИЯИ",
) -> argparse.ArgumentParser:
    """Описать проверку файлов, предварительный прогон и явную запись пакета."""

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "input", type=Path,
        help=input_help,
    )
    parser.add_argument(
        "--manifest-dir", type=Path, default=project_root / "manifests",
        help="Каталог рабочих реестров",
    )
    parser.add_argument(
        "--check-files-only", action="store_true",
        help="Проверить файлы и происхождение без прав, планирования и записи",
    )
    parser.add_argument(
        "--rights-record-id", action="append", default=[],
        help="ID существующего права для PDF (manual_download/bulk и storage); повторяемый",
    )
    parser.add_argument(
        "--metadata-rights-record-id", action="append", default=[],
        help="ID существующего права для метаданных (api/bulk и storage); повторяемый",
    )
    write_mode = parser.add_mutually_exclusive_group()
    write_mode.add_argument(
        "--dry-run", action="store_true",
        help="Проверить план без записи; используется по умолчанию",
    )
    write_mode.add_argument(
        "--commit", action="store_true",
        help="Явно разрешить запись полностью проверенного плана в реестры",
    )

    return parser


def _print_batch_summary(batch: ArticleBatch) -> None:
    """Показать объём проверенного пакета и оставшиеся решения по карточкам."""

    pdf_paths = {
        path
        for article in batch.entries
        for path in (
            article.entry["pdf"]["path"],
            *article.entry["pdf"]["aliases"],
        )
    }
    missing_genres = sum(not article.entry.get("genre") for article in batch.entries)
    missing_journals = sum(not article.entry.get("journal_id") for article in batch.entries)
    print(f"Проверены статьи: {len(batch.entries)}; путей PDF: {len(pdf_paths)}.")
    print(f"Не заполнены: genre — {missing_genres}; journal_id — {missing_journals}.")
    print("Статус допуска остаётся pending; допуск к обучению не выставляется.")


def _commit_plan(
    store: ManifestStore,
    plan: ManifestPlan,
    *,
    dry_run: bool,
    reconcile: Callable[
        [ManifestStore, ManifestPlan], tuple[ManifestPlan, dict[str, str]]
    ] | None = None,
) -> CommitResult:
    """Согласовать и проверить либо записать план с повтором при изменении снимка."""

    reconcile = reconcile if reconcile is not None else reconcile_article_plan

    for attempt in range(MAX_COMMIT_ATTEMPTS):
        try:
            reconciled, expected_hashes = reconcile(store, plan)

            return store.commit(
                reconciled, dry_run=dry_run,
                expected_snapshot_hashes=expected_hashes,
            )

        except ManifestConcurrencyError:
            if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                raise

    raise ManifestConcurrencyError("Не удалось согласовать план с текущим снимком реестров")


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить весь пакет и записать его только при явном параметре --commit."""

    root = project_root.resolve()
    parser = _build_parser(root)
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
        batch = read_article_batch(root, path)
        _print_batch_summary(batch)

        if arguments_parsed.check_files_only:
            print("Происхождение проверено; реестры и допуск по правам не изменялись.")

            return 0

        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="bulk",
            rights_record_ids=tuple(arguments_parsed.rights_record_id),
            extraction_method="not_started",
            extraction_version="jinr-article-pdf-v1",
            response_representation="pdf",
            request_context_type="work",
        )
        collected_at = datetime.now(timezone.utc).isoformat()
        plan = plan_article_batch(
            root, path, options,
            collected_at=collected_at,
            metadata_rights_record_ids=tuple(arguments_parsed.metadata_rights_record_id),
        )

        for warning in plan.warnings:
            print(f"Предупреждение: {warning}")

        store = ManifestStore(
            project_root=root,
            manifest_dir=arguments_parsed.manifest_dir,
            schema_dir=root / "manifests/schemas",
        )

        result = _commit_plan(store, plan, dry_run=not arguments_parsed.commit)
        action = "Предварительная проверка" if result.dry_run else "Регистрация"
        print(f"{action} завершена.")
        print(f"Добавлено: {result.inserted}; обновлено: {result.updated}.")

        return 0

    except (ValueError, OSError, ManifestError) as exception:
        parser.error(f"Не удалось обработать пакет статей: {exception}")


if __name__ == "__main__":
    raise SystemExit(main())
