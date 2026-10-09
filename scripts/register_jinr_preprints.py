#!/usr/bin/env python3
"""Проверить и зарегистрировать сохранённые PDF-препринты ОИЯИ без сети."""

from __future__ import annotations

import argparse
import sys

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Команда должна запускаться напрямую из scripts/ без установки пакета.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.jinr_registration import (  # noqa: E402
    plan_preprint_download,
    preprint_extraction_cards,
    read_preprint_download,
    reconcile_preprint_plan,
)
from src.corpus.manifests import (  # noqa: E402
    ManifestConcurrencyError,
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
    canonical_json,
    sha256_bytes,
)
from src.corpus.registration import (  # noqa: E402
    RegistrationOptions,
)

MAX_COMMIT_ATTEMPTS = 3


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Описать отдельную проверку файлов и регистрацию с явными правами."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", type=Path, nargs="?",
        default=project_root / "manifests/imports/jinr_api/preprint_downloads",
        help="Опись загрузки JSON либо каталог таких описей",
    )
    parser.add_argument(
        "--manifest-dir", type=Path, default=project_root / "manifests",
        help="Каталог рабочих реестров",
    )
    parser.add_argument(
        "--check-files-only", action="store_true",
        help="Проверить файлы и происхождение, не проверяя допуск по правам",
    )
    parser.add_argument(
        "--cards-output", type=Path,
        help="Новый JSONL с карточками для rebuild_from_pdf.py; не признак регистрации",
    )
    parser.add_argument(
        "--rights-record-id", action="append", default=[],
        help="ID существующего права для PDF (crawler/bulk и storage); повторяемый",
    )
    parser.add_argument(
        "--metadata-rights-record-id", action="append", default=[],
        help="ID существующего права для метаданных (api/bulk и storage); повторяемый",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Не записывать ни реестры, ни выходной JSONL",
    )

    return parser


def _input_paths(path: Path) -> list[Path]:
    """Получить непустой упорядоченный список описей загрузки."""

    if path.is_file():
        return [path]

    if not path.is_dir():
        raise ValueError(f"Опись или каталог не найдены: {path}")

    paths = sorted(path.glob("*.json"))

    if not paths:
        raise ValueError(f"В каталоге нет описей JSON: {path}")

    return paths


def _combine_plans(plans: list[ManifestPlan]) -> ManifestPlan:
    """Объединить планы, не дублируя общие HTTP-события страниц API."""

    combined = ManifestPlan()

    for field_name, primary_key in (
        ("works", "work_id"),
        ("artifacts", "artifact_record_id"),
        ("retrieval_events", "retrieval_id"),
        ("work_aliases", "alias_record_id"),
    ):
        unique = {}

        for plan in plans:
            for record in getattr(plan, field_name):
                key = record[primary_key]

                if key in unique and unique[key] != record:
                    raise ValueError(f"Противоречащие записи {field_name}: {key}")

                unique[key] = record

        getattr(combined, field_name).extend(unique.values())

    for plan in plans:
        combined.blobs.extend(plan.blobs)
        combined.warnings.extend(plan.warnings)

    return combined


def _cards_blob(project_root: Path, path: Path, data: bytes) -> PlannedBlob:
    """Ограничить экспорт карточек каталогом импорта внутри проекта."""

    destination = path if path.is_absolute() else project_root / path
    destination = destination.resolve()
    imports_root = (project_root / "manifests/imports").resolve()

    if not destination.is_relative_to(imports_root) or destination.suffix != ".jsonl":
        raise ValueError("Карточки нужно сохранить в manifests/imports/ с расширением .jsonl")

    if destination.exists() and destination.read_bytes() != data:
        raise ValueError(f"Другой файл уже существует; выберите новое имя: {destination}")

    return PlannedBlob(
        relative_path=destination.relative_to(project_root).as_posix(),
        data=data,
        sha256=sha256_bytes(data),
    )


def _save_checked_cards(project_root: Path, blob: PlannedBlob) -> None:
    """Сохранить новые карточки, не заменяя существующий файл."""

    destination = project_root / blob.relative_path

    if destination.exists():
        if destination.read_bytes() != blob.data:
            raise ValueError(f"Выходной файл изменился: {destination}")

        return

    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("xb") as output:
        output.write(blob.data)


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить происхождение всех файлов до любой записи результатов."""

    root = project_root.resolve()
    parser = _build_parser(root)
    arguments_parsed = parser.parse_args(arguments)

    if not arguments_parsed.check_files_only and (
        not arguments_parsed.rights_record_id
        or not arguments_parsed.metadata_rights_record_id
    ):
        parser.error(
            "Для регистрации нужны --rights-record-id и --metadata-rights-record-id; "
            "для проверки файлов используйте --check-files-only"
        )

    paths = _input_paths(arguments_parsed.input.resolve())
    downloads = [read_preprint_download(root, path) for path in paths]
    cards = [
        card
        for download in downloads
        for card in preprint_extraction_cards(download)
    ]
    cards_blob = None

    if arguments_parsed.cards_output:
        cards_data = "".join(canonical_json(asdict(card)) + "\n" for card in cards).encode("utf-8")
        cards_blob = _cards_blob(root, arguments_parsed.cards_output, cards_data)

    if arguments_parsed.check_files_only:
        if cards_blob is not None and not arguments_parsed.dry_run:
            _save_checked_cards(root, cards_blob)

        print(f"Проверены описи: {len(downloads)}; PDF: {len(cards)}.")
        print("Происхождение проверено; реестры и допуск по правам не изменялись.")

        return 0

    options = RegistrationOptions(
        content_role="full_text",
        acquisition_method="crawler",
        acquisition_scope="bulk",
        rights_record_ids=tuple(arguments_parsed.rights_record_id),
        extraction_method="not_started",
        extraction_version="jinr-preprint-pdf-v1",
        response_representation="pdf",
        request_context_type="work",
    )
    collected_at = datetime.now(timezone.utc).isoformat()
    plan = _combine_plans([
        plan_preprint_download(
            root, path, options,
            collected_at=collected_at,
            metadata_rights_record_ids=tuple(arguments_parsed.metadata_rights_record_id),
        )
        for path in paths
    ])

    if cards_blob is not None:
        plan.blobs.append(cards_blob)

    store = ManifestStore(
        project_root=root,
        manifest_dir=arguments_parsed.manifest_dir,
        schema_dir=root / "manifests/schemas",
    )

    for attempt in range(MAX_COMMIT_ATTEMPTS):
        reconciled, expected_hashes = reconcile_preprint_plan(store, plan)

        try:
            result = store.commit(
                reconciled, dry_run=arguments_parsed.dry_run,
                expected_snapshot_hashes=expected_hashes,
            )
            break

        except ManifestConcurrencyError:
            if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                raise

    action = "Предварительная проверка" if result.dry_run else "Регистрация"
    print(f"{action} завершена: {len(downloads)} описей, {len(cards)} PDF.")
    print(f"Добавлено: {result.inserted}; обновлено: {result.updated}.")
    print("Допуск к обучению не выставлялся.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
