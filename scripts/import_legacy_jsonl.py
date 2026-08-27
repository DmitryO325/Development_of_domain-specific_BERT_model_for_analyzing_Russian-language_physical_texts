#!/usr/bin/env python3
"""Безопасный импорт старого corpus.jsonl в works/artifacts/rights v1.

По умолчанию выполняется только проверка. Для записи нужен явный --commit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collect.base import Document  # noqa: E402
from src.corpus.legacy import plan_legacy_document  # noqa: E402
from src.corpus.manifests import (  # noqa: E402
    ManifestError,
    ManifestPlan,
    ManifestStore,
    sha256_file,
)
from src.corpus.profiles import SOURCE_PROFILES, get_source_profile  # noqa: E402
from src.corpus.schema_validation import SchemaValidationError  # noqa: E402

DOCUMENT_FIELDS = {item.name for item in fields(Document)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Импорт старого JSONL сборщика в машинные реестры корпуса"
    )
    parser.add_argument("input", type=Path, help="Старый JSONL с объектами Document")
    parser.add_argument(
        "--profile",
        choices=["auto", *sorted(SOURCE_PROFILES)],
        default="auto",
        help="Профиль источника; auto выбирает его по source и URL",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=ROOT / "manifests",
        help="Каталог рабочих реестров",
    )
    parser.add_argument(
        "--rights-record-id",
        action="append",
        default=[],
        help="Применимая запись rights.jsonl; параметр можно повторять",
    )
    parser.add_argument(
        "--acquisition-method",
        required=True,
        choices=["manual_download", "api", "crawler", "platform_export", "other"],
        help="Фактический способ получения этого входного набора",
    )
    parser.add_argument(
        "--acquisition-scope",
        required=True,
        choices=["single", "sample", "bulk"],
        help="Фактический масштаб получения",
    )
    parser.add_argument(
        "--imported-at",
        help="Фиксированный ISO datetime; иначе используется mtime входного JSONL",
    )
    parser.add_argument("--skip-pdf", action="store_true", help="Не регистрировать локальные PDF")
    parser.add_argument("--limit", type=int, help="Проверить/импортировать первые N строк")
    parser.add_argument(
        "--allow-unresolved-rights",
        action="store_true",
        help="Только для предпросмотра: показать технический план без записей прав",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Записать данные; без флага выполняется только предпросмотр",
    )
    return parser.parse_args()


def document_from_record(record: object, *, line_number: int) -> Document:
    if not isinstance(record, dict):
        raise ValueError(f"строка {line_number}: ожидается JSON-объект")
    unknown = sorted(set(record) - DOCUMENT_FIELDS)
    if unknown:
        raise ValueError(
            f"строка {line_number}: неизвестные поля Document: {', '.join(unknown)}"
        )
    required_strings = ("source", "url", "title", "text")
    for key in required_strings:
        if not isinstance(record.get(key), str):
            raise ValueError(f"строка {line_number}: поле {key} должно быть строкой")
    if "authors" in record and (
        not isinstance(record["authors"], list)
        or not all(isinstance(value, str) for value in record["authors"])
    ):
        raise ValueError(f"строка {line_number}: authors должен быть списком строк")
    if "extra" in record and not isinstance(record["extra"], dict):
        raise ValueError(f"строка {line_number}: extra должен быть объектом")
    for key in ("published", "section", "pdf_url"):
        if key in record and record[key] is not None and not isinstance(record[key], str):
            raise ValueError(f"строка {line_number}: поле {key} должно быть строкой или null")
    try:
        return Document(**record)
    except TypeError as exc:
        raise ValueError(f"строка {line_number}: несовместимая запись Document: {exc}") from exc


def default_timestamp(path: Path) -> str:
    value = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return value.isoformat(timespec="seconds")


def iter_plans(
    *,
    args: argparse.Namespace,
    input_path: Path,
    timestamp: str,
    warnings: list[str] | None,
    stats: dict[str, int],
):
    examined = 0
    with input_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            if args.limit is not None and examined >= args.limit:
                break
            examined += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"строка {line_number}: некорректный JSON: {exc}") from exc
            document = document_from_record(record, line_number=line_number)
            profile = get_source_profile(
                args.profile,
                source=document.source,
                url=document.url,
            )
            plan = plan_legacy_document(
                document,
                profile=profile,
                project_root=ROOT,
                imported_at=timestamp,
                acquisition_method=args.acquisition_method,
                acquisition_scope=args.acquisition_scope,
                rights_record_ids=args.rights_record_id,
                include_pdf=not args.skip_pdf,
            )
            stats["processed"] += 1
            if warnings is not None:
                warnings.extend(plan.warnings)
            yield plan


def main() -> int:
    args = parse_args()
    if args.commit and args.allow_unresolved_rights:
        print(
            "--allow-unresolved-rights разрешён только без --commit",
            file=sys.stderr,
        )
        return 2
    input_path = args.input.resolve()
    if not input_path.is_file():
        print(f"Файл не найден: {input_path}", file=sys.stderr)
        return 2
    timestamp = args.imported_at or default_timestamp(input_path)
    input_size = input_path.stat().st_size
    input_sha256 = sha256_file(input_path)
    store = ManifestStore(project_root=ROOT, manifest_dir=args.manifest_dir)

    stats = {"processed": 0}
    warnings: list[str] = []
    mode = "ЗАПИСЬ" if args.commit else "ПРЕДПРОСМОТР"
    print(
        f"Режим: {mode}; вход: {input_path}; "
        f"SHA-256: {input_sha256}",
        flush=True,
    )

    try:
        preview = store.preflight(
            iter_plans(
                args=args,
                input_path=input_path,
                timestamp=timestamp,
                warnings=warnings,
                stats=stats,
            ),
            allow_unresolved_rights=args.allow_unresolved_rights,
        )
        if (
            input_path.stat().st_size != input_size
            or sha256_file(input_path) != input_sha256
        ):
            raise ManifestError(
                "Входной JSONL изменился во время предварительной проверки"
            )
    except (ValueError, OSError, ManifestError, SchemaValidationError) as exc:
        print(f"Предварительная проверка не пройдена: {exc}", file=sys.stderr)
        if not args.rights_record_id and not args.allow_unresolved_rights:
            print(
                "Для технической проверки без прав добавьте "
                "--allow-unresolved-rights; этот флаг не разрешает --commit.",
                file=sys.stderr,
            )
        print("Ничего не записано.", file=sys.stderr)
        return 1

    if stats["processed"] == 0:
        print("Во входном файле нет записей для импорта.", file=sys.stderr)
        return 1

    result = preview
    if args.commit:
        batch = ManifestPlan()
        written_blobs = unchanged_blobs = 0
        second_pass_stats = {"processed": 0}
        try:
            if (
                input_path.stat().st_size != input_size
                or sha256_file(input_path) != input_sha256
            ):
                raise ManifestError(
                    "Входной JSONL изменился после предварительной проверки"
                )
            for plan in iter_plans(
                args=args,
                input_path=input_path,
                timestamp=timestamp,
                warnings=None,
                stats=second_pass_stats,
            ):
                # Вызывается только после полного preflight и сверки SHA-256
                # входа; публичного API записи blob без плана нет.
                written, same = store._write_blobs(plan.blobs)
                written_blobs += written
                unchanged_blobs += same
                batch.rights.extend(plan.rights)
                batch.works.extend(plan.works)
                batch.artifacts.extend(plan.artifacts)
            if (
                input_path.stat().st_size != input_size
                or sha256_file(input_path) != input_sha256
            ):
                raise ManifestError(
                    "Входной JSONL изменился во время второго прохода; "
                    "реестры не будут изменены"
                )
            committed = store.commit(batch)
            result = type(preview)(
                inserted=committed.inserted,
                unchanged=committed.unchanged,
                written_blobs=written_blobs,
                unchanged_blobs=unchanged_blobs,
                dry_run=False,
            )
        except (ValueError, OSError, ManifestError, SchemaValidationError) as exc:
            print(f"Запись не завершена: {exc}", file=sys.stderr)
            print(
                "Могли остаться безопасные файлы по SHA-256 или уже атомарно "
                "завершённые ранние реестры. Проверьте validate_manifests.py; после "
                "устранения причины тот же пакет можно повторить без дублирования.",
                file=sys.stderr,
            )
            return 1

    for warning in list(dict.fromkeys(warnings))[:10]:
        print(f"Предупреждение: {warning}")
    if len(set(warnings)) > 10:
        print(f"Предупреждения: показано 10 из {len(set(warnings))}")

    action = "будет добавлено" if not args.commit else "добавлено"
    print(
        f"Обработано: {stats['processed']}; {action} работ: {result.inserted['works']}; "
        f"артефактов: {result.inserted['artifacts']}; "
        f"неизменённых записей: {sum(result.unchanged.values())}"
    )
    if not args.commit:
        if args.allow_unresolved_rights:
            print(
                "Реестры и data/ не изменены. Это техническая инвентаризация: "
                "до --commit создайте записи прав и повторите проверку без "
                "--allow-unresolved-rights."
            )
        else:
            print("Реестры и data/ не изменены. Для записи повторите команду с --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
