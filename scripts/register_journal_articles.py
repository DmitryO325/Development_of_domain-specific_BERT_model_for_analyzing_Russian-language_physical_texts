#!/usr/bin/env python3
"""Проверка и регистрация сохранённых партий журналов без сетевых запросов."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from datetime import datetime, timezone
from dataclasses import asdict
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Команда должна находить модули проекта при запуске напрямую из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.journal_registration import (  # noqa: E402
    combine_article_plans,
    load_articles,
    plan_collected_article,
)
from src.corpus.manifests import CommitResult, ManifestConcurrencyError, ManifestError, ManifestStore  # noqa: E402
from src.corpus.registration import reconcile_document_plan, resolve_collection_rights  # noqa: E402
from src.corpus.journal_batch_input import read_frozen_journal_batch  # noqa: E402
from src.corpus.journal_batch_registration import read_batch_assessment, reconcile_batch_registration  # noqa: E402
from src.corpus.journal_inventory_input import prepare_inventory_batch, read_inventory_batch  # noqa: E402
from src.corpus.journal_inventory_registration import (  # noqa: E402
    deferred_source_report, reconcile_inventory_registration,
)

SOURCES = ("ioffe", "jetp", "jetp_letters", "quantum", "ufn")
MAX_COMMIT_ATTEMPTS = 3


def build_parser() -> argparse.ArgumentParser:
    """Разделить техническую проверку исходников и явную запись в реестры."""

    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("source", choices=SOURCES)
    common.add_argument("--input-dir", type=Path, help="Каталог партии или карточек JSON")
    common.add_argument("--raw-dir", type=Path, help="Корень исходных HTTP-ответов")
    common.add_argument("--manifest-dir", type=Path, default=PROJECT_ROOT / "manifests")
    common.add_argument("--max-articles", type=int, default=0, help="0 — все сохранённые карточки")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", parents=[common], help="Проверить исходники, не меняя файлы")
    register = commands.add_parser("register", parents=[common], help="Записать пакет в общие реестры")
    register.add_argument("--rights-record-id", action="append", required=True,
                          help="Существующая запись основания; параметр можно повторять")
    register.add_argument("--dry-run", action="store_true", help="Проверить регистрацию без записи")
    batch = commands.add_parser("register-batch", help="Импортировать проверенный фиксированный состав по DEC-012")
    batch.add_argument("batch", type=Path, help="JSON со списком карточек и их SHA-256")
    batch.add_argument("--assessment", type=Path, required=True, help="Явная проверка оснований для этой партии")
    batch.add_argument("--accept-dec012", action="store_true", required=True,
                       help="Подтвердить условное основание только для сбора и хранения перечисленных снимков")
    batch.add_argument("--manifest-dir", type=Path, default=PROJECT_ROOT / "manifests")
    batch.add_argument("--dry-run", action="store_true", help="Проверить весь пакет без записи")
    prepare = commands.add_parser("prepare-inventory", help="Зафиксировать проверяемую партию без сети и регистрации")
    prepare.add_argument("--output-dir", type=Path, required=True, help="Новый каталог неизменяемой партии")
    prepare.add_argument("--source", choices=SOURCES, action="append", help="По умолчанию все пять источников")
    inventory = commands.add_parser("register-inventory", help="Учесть полученные файлы без подтверждения прав и допуска")
    inventory.add_argument("batch", type=Path, help="batch.json ранее подготовленной партии")
    inventory.add_argument("--manifest-dir", type=Path, default=PROJECT_ROOT / "manifests")
    inventory.add_argument("--dry-run", action="store_true", help="Проверить план, не записывая реестры и квитанцию")

    return parser


def main(arguments: list[str] | None = None) -> int:
    """Проверить всю выбранную партию до атомарной записи в рабочие реестры."""

    options = build_parser().parse_args(arguments)

    if options.command == "prepare-inventory":
        return _prepare_inventory(options)

    if options.command == "register-inventory":
        return _register_inventory(options)

    if options.command == "register-batch":
        return _register_batch(options)

    suffix = Path("ioffe") if options.source == "ioffe" else Path("journals") / options.source
    input_dir = options.input_dir or PROJECT_ROOT / "manifests" / "imports" / suffix
    raw_root = options.raw_dir or PROJECT_ROOT / "data" / "raw" / suffix

    try:
        articles = load_articles(input_dir, source=options.source, raw_root=raw_root,
                                 max_articles=options.max_articles)
        pdf_count = sum(article.pdf_response is not None for article in articles)
        print(f"Проверено русских карточек: {len(articles)}; с PDF: {pdf_count}; без PDF: {len(articles) - pdf_count}.")

        if options.command == "check":
            print("Это проверка исходников, не регистрация и не допуск к обучению. Файлы не изменены.")
            return 0

        if not articles:
            raise ValueError("Нет русских статей для регистрации")

        store = ManifestStore(project_root=PROJECT_ROOT, manifest_dir=options.manifest_dir,
                              schema_dir=PROJECT_ROOT / "manifests" / "schemas")
        rights_by_profile = {}

        for article in articles:
            profile = article.profile

            if profile.key not in rights_by_profile:
                rights_by_profile[profile.key] = resolve_collection_rights(
                    store, profile, acquisition_method="crawler", acquisition_scope="bulk",
                    allowed_rights_record_ids=tuple(options.rights_record_id),
                )

        registered_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        plan = combine_article_plans([
            plan_collected_article(article, rights_record_ids=rights_by_profile[article.profile.key],
                                   registered_at=registered_at)
            for article in articles
        ])
        result: CommitResult | None = None

        for attempt in range(MAX_COMMIT_ATTEMPTS):
            reconciled, expected_hashes = reconcile_document_plan(store, plan)

            try:
                result = store.commit(reconciled, dry_run=options.dry_run, expected_snapshot_hashes=expected_hashes)
                break

            except ManifestConcurrencyError:
                if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                    raise

        if result is None:
            raise ManifestConcurrencyError("Регистрация не завершена из-за параллельного изменения реестров")

        label = "Предварительная проверка регистрации" if options.dry_run else "Регистрация"
        print(f"{label} завершена: новых работ — {result.inserted['works']}, "
              f"артефактов — {result.inserted['artifacts']}, событий — {result.inserted['retrieval_events']}.")
        print("Допуск к обучению не менялся. Исходные карточки сборщиков не перезаписывались.")

        return 0

    except (ManifestError, OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: {exception}. Пакет не зарегистрирован.", file=sys.stderr)
        return 1


def _register_batch(options: argparse.Namespace) -> int:
    """Проверить фиксированный состав и записать только узкие основания этой партии."""

    try:
        batch = read_frozen_journal_batch(options.batch, project_root=PROJECT_ROOT)
        assessment = read_batch_assessment(options.assessment, batch)
        store = ManifestStore(project_root=PROJECT_ROOT, manifest_dir=options.manifest_dir,
                              schema_dir=PROJECT_ROOT / "manifests" / "schemas")

        for attempt in range(MAX_COMMIT_ATTEMPTS):
            plan, expected_hashes = reconcile_batch_registration(store, batch, assessment)

            try:
                result = store.commit(plan, dry_run=options.dry_run, expected_snapshot_hashes=expected_hashes)
                label = "Предварительная проверка" if options.dry_run else "Регистрация"
                print(f"{label}: карточек в партии — {len(batch.records)}; "
                      f"новых работ — {result.inserted['works']}, артефактов — {result.inserted['artifacts']}, "
                      f"событий — {result.inserted['retrieval_events']}, оснований — {result.inserted['rights']}.")
                print("Основания ограничены проверенными файлами и событиями; допуск к обучению не менялся.")

                return 0

            except ManifestConcurrencyError:
                if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                    raise

        raise ManifestConcurrencyError("Регистрация партии не завершена")

    except (ManifestError, OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: {exception}. Пакет не зарегистрирован.", file=sys.stderr)
        return 1


def _prepare_inventory(options: argparse.Namespace) -> int:
    """Зафиксировать исходные карточки отдельно от продолжающегося скачивания."""

    try:
        path = prepare_inventory_batch(
            project_root=PROJECT_ROOT,
            output_dir=options.output_dir,
            sources=tuple(options.source or SOURCES),
        )
        print(f"Партия зафиксирована: {path}. Рабочие реестры не изменены.")

        return 0

    except (ManifestError, OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: {exception}. Подготовка партии не завершена.", file=sys.stderr)
        return 1


def _write_inventory_receipt(batch_path: Path, payload: dict[str, Any]) -> Path:
    """Сохранить отдельную квитанцию запуска, не перезаписывая прежние результаты."""

    directory = batch_path.parent / "receipts"

    if directory.is_symlink():
        raise ValueError("Каталог квитанций не должен быть символической ссылкой")

    directory.mkdir(exist_ok=True)
    destination = directory / f"{uuid4().hex}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=directory)
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.link(temporary_path, destination)

    finally:
        temporary_path.unlink(missing_ok=True)

    return destination


def _register_inventory(options: argparse.Namespace) -> int:
    """Внести проверенные факты получения без создания положительных прав."""

    try:
        print("Проверяю закреплённые карточки и исходные HTTP-ответы...", flush=True)
        batch = read_inventory_batch(options.batch, project_root=PROJECT_ROOT)
        print(
            f"Проверено карточек: {len(batch.records)}; исключено нерусских: {len(batch.skipped)}; "
            f"отложено конфликтов исходников: {len(batch.deferred)}.", flush=True,
        )
        store = ManifestStore(project_root=PROJECT_ROOT, manifest_dir=options.manifest_dir,
                              schema_dir=PROJECT_ROOT / "manifests" / "schemas")
        registered_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

        for attempt in range(MAX_COMMIT_ATTEMPTS):
            print("Сопоставляю работы, файлы и прежние события получения...", flush=True)
            plan, expected_hashes, report = reconcile_inventory_registration(
                store, batch.records, registered_at=registered_at,
            )
            report.extend(deferred_source_report(batch.deferred, PROJECT_ROOT))

            try:
                print("Проверяю целостность общего плана и реестров...", flush=True)
                result = store.commit(plan, dry_run=options.dry_run, expected_snapshot_hashes=expected_hashes)
                break

            except ManifestConcurrencyError:
                if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                    raise

        else:
            raise ManifestConcurrencyError("Инвентарная регистрация не завершена")

    except (ManifestError, OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: {exception}. Инвентарная партия не зарегистрирована.", file=sys.stderr)
        return 1

    label = "Предварительная проверка" if options.dry_run else "Инвентарная регистрация"
    print(f"{label}: новых работ — {result.inserted['works']}, артефактов — {result.inserted['artifacts']}, "
          f"событий — {result.inserted['retrieval_events']}; записей в отчёте — {len(report)}.")
    print("Новые материалы учитываются в карантине. Права, исходники и допуск существующих работ не менялись.")

    report_counts = Counter(item["status"] for item in report)
    print(f"Уже зарегистрированы: {report_counts['already_registered']}; "
          f"повторы внутри партии: {report_counts['skipped']}; отложены: {report_counts['deferred']}.")

    for item in report:
        if item["status"] == "deferred":
            print(f"  Отложено: {item['url']} — {item['reason']}")

    if options.dry_run:
        return 0

    try:
        receipt = _write_inventory_receipt(batch.path, {
            "schema_version": "journal-inventory-receipt-v1",
            "batch_sha256": batch.sha256,
            "registered_at": registered_at,
            "previous_snapshot_sha256": expected_hashes,
            "result": asdict(result),
            "report": report,
            "skipped_non_russian": list(batch.skipped),
            "deferred_source": list(batch.deferred),
            "rights_changed": False,
            "training_eligibility_changed": False,
        })
        print(f"Квитанция: {receipt}")

    except (OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: реестры записаны, но квитанция не сохранена: {exception}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
