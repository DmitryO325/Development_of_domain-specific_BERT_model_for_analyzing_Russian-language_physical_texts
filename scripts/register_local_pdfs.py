#!/usr/bin/env python3
"""Зарегистрировать вручную загруженные PDF в реестрах корпуса."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.local_registration import (  # noqa: E402
    LocalFileRegistration,
    plan_local_file,
)
from src.corpus.manifests import (  # noqa: E402
    ManifestConcurrencyError,
    ManifestPlan,
    ManifestStore,
)
from src.corpus.profiles import SOURCE_PROFILES, get_source_profile  # noqa: E402
from src.corpus.registration import (  # noqa: E402
    RegistrationOptions,
    reconcile_document_plan,
    resolve_collection_rights,
)

MAX_COMMIT_ATTEMPTS = 3


class _DuplicateJsonKeyError(ValueError):
    """JSON-объект содержит один ключ более одного раза."""


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Собрать JSON-объект и отклонить повторяющиеся ключи."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"повторный ключ {key!r}")

        result[key] = value

    return result


def _read_registrations(path: Path) -> list[LocalFileRegistration]:
    """Прочитать непустой JSONL с карточками локальных PDF."""

    if not path.is_file():
        raise ValueError(f"Файл карточек не найден: {path}")

    registrations: list[LocalFileRegistration] = []

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue

        try:
            record = json.loads(
                raw_line,
                object_pairs_hook=_object_without_duplicate_keys,
            )

        except (json.JSONDecodeError, _DuplicateJsonKeyError) as exception:
            raise ValueError(
                f"{path}:{line_number}: некорректный JSON: {exception}"
            ) from exception

        if not isinstance(record, dict):
            raise ValueError(
                f"{path}:{line_number}: строка должна содержать JSON-объект"
            )

        try:
            registration = LocalFileRegistration(**record)

        except TypeError as exception:
            raise ValueError(
                f"{path}:{line_number}: неверный набор полей карточки: {exception}"
            ) from exception

        registrations.append(registration)

    if not registrations:
        raise ValueError(f"Файл карточек пуст: {path}")

    return registrations


def _combine_plans(plans: list[ManifestPlan]) -> ManifestPlan:
    """Объединить независимые планы PDF в один атомарный пакет."""

    combined = ManifestPlan()

    for plan in plans:
        combined.works.extend(plan.works)
        combined.artifacts.extend(plan.artifacts)
        combined.retrieval_events.extend(plan.retrieval_events)
        combined.work_aliases.extend(plan.work_aliases)
        combined.warnings.extend(plan.warnings)

    return combined


def _build_parser() -> argparse.ArgumentParser:
    """Построить интерфейс команды регистрации локальных PDF."""

    parser = argparse.ArgumentParser(
        description="Регистрация вручную загруженных PDF в реестрах корпуса"
    )
    parser.add_argument(
        "input",
        type=Path,
        help="JSONL-файл с карточками локальных PDF",
    )
    parser.add_argument(
        "--profile",
        required=True,
        choices=sorted(SOURCE_PROFILES),
        help="Явный профиль источника",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=PROJECT_ROOT / "manifests",
        help="Каталог рабочих JSONL-реестров",
    )
    parser.add_argument(
        "--rights-record-id",
        action="append",
        required=True,
        help="Явно разрешённый ID записи rights; параметр можно повторять",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Полностью проверить пакет без изменения реестров",
    )

    return parser


def _print_result(result: Any) -> None:
    """Вывести основные счётчики проверки или записи."""

    action = "Предварительная проверка" if result.dry_run else "Регистрация"
    print(f"{action} завершена.")

    for label, counts in (
        ("Добавлено", result.inserted),
        ("Обновлено", result.updated),
        ("Без изменений", result.unchanged),
    ):
        nonzero = [
            f"{kind}={count}"
            for kind, count in sorted(counts.items())
            if count
        ]

        if nonzero:
            print(f"{label}: {', '.join(nonzero)}")

    print(
        "Файловые объекты: "
        f"новых={result.written_blobs}, "
        f"без изменений={result.unchanged_blobs}"
    )


def main(arguments: list[str] | None = None) -> int:
    """Проверить права и атомарно зарегистрировать локальные PDF."""

    options = _build_parser().parse_args(arguments)
    profile = get_source_profile(options.profile)
    store = ManifestStore(
        project_root=PROJECT_ROOT,
        manifest_dir=options.manifest_dir,
        schema_dir=PROJECT_ROOT / "manifests" / "schemas",
    )
    rights_record_ids = resolve_collection_rights(
        store,
        profile,
        acquisition_method="manual_download",
        acquisition_scope="sample",
        allowed_rights_record_ids=tuple(options.rights_record_id),
    )
    registration_options = RegistrationOptions(
        content_role="full_text",
        acquisition_method="manual_download",
        acquisition_scope="sample",
        rights_record_ids=rights_record_ids,
        extraction_method="not_started",
        extraction_version="manual-pdf-v1",
        response_representation="pdf",
        request_context_type="work",
    )
    registrations = _read_registrations(options.input)
    plan = _combine_plans(
        [
            plan_local_file(
                registration,
                profile,
                registration_options,
                project_root=PROJECT_ROOT,
            )
            for registration in registrations
        ]
    )

    for attempt in range(MAX_COMMIT_ATTEMPTS):
        reconciled, expected_hashes = reconcile_document_plan(store, plan)

        try:
            result = store.commit(
                reconciled,
                dry_run=options.dry_run,
                expected_snapshot_hashes=expected_hashes,
            )
            break

        except ManifestConcurrencyError as exception:
            if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                raise ManifestConcurrencyError(
                    "Реестры несколько раз изменились параллельно; "
                    "PDF не зарегистрированы"
                ) from exception

    _print_result(result)
    print(f"Карточек PDF: {len(registrations)}")
    print(f"Реестры: {store.manifest_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
