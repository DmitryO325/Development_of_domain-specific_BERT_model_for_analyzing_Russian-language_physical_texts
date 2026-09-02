#!/usr/bin/env python3
"""Извлечь и зарегистрировать текст из ранее зарегистрированных PDF."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.pdf_text import (  # noqa: E402
    PdfPageExportResult,
    PdfTextExtraction,
    cyrillic_letter_ratio,
    export_pdf_pages,
    extract_best_text_result,
)
from src.corpus.extraction_registration import (  # noqa: E402
    find_registered_pdf_artifact,
    normalize_extraction_version,
    plan_extracted_text,
)
from src.corpus.local_registration import (  # noqa: E402
    LocalFileRegistration,
    read_local_file_registrations,
)
from src.corpus.manifests import (  # noqa: E402
    ManifestPlan,
    ManifestStore,
    canonical_json,
)

DEFAULT_EXTRACTION_VERSION = "pdf-text-v1"
EXTRACTION_REPORT_SCHEMA_VERSION = "extraction-pilot-v2"
EXPECTED_EXTRACTION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class ExtractionFunction(Protocol):
    """Сигнатура функции выбора текста из одного PDF-файла."""

    def __call__(
        self,
        pdf_path: Path,
        *,
        text_dir: Path | None = None,
        try_ocr: bool = True,
    ) -> PdfTextExtraction | tuple[str, str, bool]:
        """Вернуть полный или совместимый результат извлечения."""

        ...


class PageExportFunction(Protocol):
    """Сигнатура функции записи готового постраничного результата."""

    def __call__(
        self,
        pdf_path: Path,
        extraction: PdfTextExtraction,
        output_dir: Path,
        *,
        extraction_version: str,
        source_pdf_path: str | None = None,
        source_pdf_sha256: str | None = None,
    ) -> PdfPageExportResult:
        """Сохранить страницы без повторного извлечения или OCR."""

        ...


def _positive_integer(value: str) -> int:
    """Разобрать положительное целое число для аргумента CLI."""

    try:
        parsed_value = int(value)

    except ValueError as exception:
        raise argparse.ArgumentTypeError("ожидается целое число") from exception

    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("значение должно быть положительным")

    return parsed_value


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Построить интерфейс команды извлечения зарегистрированных PDF."""

    parser = argparse.ArgumentParser(
        description=(
            "Извлечение текста из зарегистрированных локальных PDF "
            "с записью производных артефактов"
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="JSONL-файл с карточками LocalFileRegistration",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=project_root / "manifests",
        help="Каталог рабочих JSONL-реестров",
    )
    parser.add_argument(
        "--extraction-version",
        default=DEFAULT_EXTRACTION_VERSION,
        help="Версия алгоритма извлечения и каталог в data/extracted/",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help=(
            "JSONL-отчёт пилота; по умолчанию "
            "manifests/results/<имя_входа>_extraction.jsonl"
        ),
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Не использовать Tesseract, если встроенный текст нечитаем",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=_positive_integer,
        help="Обработать только первые N карточек",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Проверить полный пакет без изменения реестров и отчёта",
    )
    parser.add_argument(
        "--export-pages",
        action="store_true",
        help="Сохранить отдельный чистый TXT и SHA-256 для каждой страницы",
    )
    parser.add_argument(
        "--page-output-dir",
        type=Path,
        default=project_root / "data" / "qa" / "ocr" / "page_exports",
        help="Корневой каталог постраничного экспорта",
    )

    return parser


def _project_path(path: Path, *, project_root: Path) -> Path:
    """Разрешить относительный CLI-путь от корня проекта."""

    return path if path.is_absolute() else project_root / path


def _default_report_path(input_path: Path, manifest_dir: Path) -> Path:
    """Построить локальный путь отчёта по имени входного файла."""

    return manifest_dir / "results" / f"{input_path.stem}_extraction.jsonl"


def _unpack_extraction(
    value: PdfTextExtraction | tuple[str, str, bool],
) -> tuple[str, str, bool, PdfTextExtraction | None]:
    """Привести полный и совместимый результаты к общей форме."""

    if isinstance(value, PdfTextExtraction):
        return value.text, value.method, value.readable, value

    text, method, readable = value

    if not isinstance(text, str) or not isinstance(method, str):
        raise TypeError("Функция извлечения должна вернуть текст и метод строками")

    if not isinstance(readable, bool):
        raise TypeError("Признак читаемости должен иметь тип bool")

    return text, method, readable, None


def _page_export_directory(
    page_output_root: Path,
    *,
    source_pdf_sha256: str,
    extraction_version: str,
    extraction_method: str,
) -> Path:
    """Построить устойчивый каталог варианта для одного исходного PDF."""

    if len(source_pdf_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in source_pdf_sha256
    ):
        raise ValueError("У исходного PDF отсутствует корректный SHA-256")

    return (
        page_output_root
        / extraction_version
        / source_pdf_sha256
        / extraction_method
    )


def _path_for_report(path: Path, *, project_root: Path) -> str:
    """Вернуть путь относительно проекта, если файл находится внутри него."""

    resolved_path = path.resolve()

    if resolved_path.is_relative_to(project_root):
        return resolved_path.relative_to(project_root).as_posix()

    return resolved_path.as_posix()


def _export_pages_for_pdf(
    pdf_path: Path,
    extraction: PdfTextExtraction,
    parent_artifact: dict[str, Any],
    *,
    page_output_root: Path,
    extraction_version: str,
    page_exporter: PageExportFunction,
) -> PdfPageExportResult:
    """Сохранить постраничный результат в каталоге исходника и варианта."""

    source_pdf_path = parent_artifact.get("path")
    source_pdf_sha256 = parent_artifact.get("sha256")

    if not isinstance(source_pdf_path, str) or not source_pdf_path:
        raise ValueError("У исходного PDF отсутствует путь в реестре")

    if not isinstance(source_pdf_sha256, str):
        raise ValueError("У исходного PDF отсутствует SHA-256 в реестре")

    output_dir = _page_export_directory(
        page_output_root,
        source_pdf_sha256=source_pdf_sha256,
        extraction_version=extraction_version,
        extraction_method=extraction.method,
    )

    return page_exporter(
        pdf_path,
        extraction,
        output_dir,
        extraction_version=extraction_version,
        source_pdf_path=source_pdf_path,
        source_pdf_sha256=source_pdf_sha256,
    )


def _combine_plans(plans: list[ManifestPlan]) -> ManifestPlan:
    """Объединить независимые текстовые артефакты в один атомарный пакет."""

    combined = ManifestPlan()

    for plan in plans:
        combined.artifacts.extend(plan.artifacts)
        combined.blobs.extend(plan.blobs)
        combined.artifact_update_reasons.update(plan.artifact_update_reasons)

    return combined


def _validate_unique_registrations(
    registrations: list[LocalFileRegistration],
    *,
    project_root: Path,
) -> None:
    """Отклонить повторную карточку одного PDF до запуска извлечения."""

    seen_paths: set[Path] = set()

    for registration in registrations:
        path = Path(registration.relative_path)
        normalized_path = (
            path.resolve()
            if path.is_absolute()
            else (project_root / path).resolve()
        )

        if normalized_path in seen_paths:
            raise ValueError(
                "Файл карточек содержит повторный relative_path="
                f"{registration.relative_path!r}"
            )

        seen_paths.add(normalized_path)


def _remember_artifact(
    artifacts: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> None:
    """Заменить известную проекцию артефакта или добавить новую."""

    for index, artifact in enumerate(artifacts):
        if artifact.get("artifact_id") == candidate["artifact_id"]:
            artifacts[index] = candidate
            return

    artifacts.append(candidate)


def _tesseract_version() -> str:
    """Вернуть краткую версию фактически используемого Tesseract."""

    try:
        import pytesseract

    except ImportError as exception:
        raise ImportError("Установите: pip install pytesseract") from exception

    version = str(pytesseract.get_tesseract_version()).splitlines()[0].strip()

    if not version:
        raise RuntimeError("Не удалось определить версию Tesseract")

    return version


def _report_record(
    registration: LocalFileRegistration,
    *,
    extracted_at: str,
    status: str,
    parent_artifact: dict[str, Any] | None = None,
    text: str = "",
    extraction_method: str | None = None,
    extraction_version: str | None = None,
    ocr_attempted: bool = False,
    ocr_version: str | None = None,
    automatic_readability: bool | None = None,
    text_artifact: dict[str, Any] | None = None,
    reused: bool = False,
    page_export_manifest_path: str | None = None,
    page_exported_pages: int | None = None,
    exception: Exception | None = None,
) -> dict[str, Any]:
    """Собрать одну строку локального отчёта о качестве извлечения."""

    if automatic_readability is None:
        readability_status = "not_evaluated"

    elif automatic_readability:
        readability_status = "passed"

    else:
        readability_status = "failed"

    if text_artifact:
        ocr_method = text_artifact.get("ocr_method")

    elif ocr_attempted:
        ocr_method = "tesseract"

    else:
        ocr_method = None

    record: dict[str, Any] = {
        "schema_version": EXTRACTION_REPORT_SCHEMA_VERSION,
        "extracted_at": extracted_at,
        "relative_path": registration.relative_path,
        "title": registration.title,
        "work_id": parent_artifact.get("work_id") if parent_artifact else None,
        "parent_artifact_id": (
            parent_artifact.get("artifact_id") if parent_artifact else None
        ),
        "extraction_method": extraction_method,
        "extraction_version": (
            text_artifact.get("extraction_version")
            if text_artifact
            else extraction_version
        ),
        "ocr_method": ocr_method,
        "ocr_version": (
            text_artifact.get("ocr_version") if text_artifact else ocr_version
        ),
        "ocr_attempted": ocr_attempted,
        "characters": len(text),
        "words": len(text.split()),
        "cyrillic_letter_ratio": cyrillic_letter_ratio(text),
        "automatic_readability": readability_status,
        "artifact_id": text_artifact.get("artifact_id") if text_artifact else None,
        "artifact_path": text_artifact.get("path") if text_artifact else None,
        "artifact_reused": reused,
        "status": status,
        "review_status": "pending" if text_artifact else "not_available",
        "error_code": type(exception).__name__ if exception else None,
        "error_detail": str(exception) if exception else None,
    }

    if page_export_manifest_path is not None:
        record["page_export_manifest_path"] = page_export_manifest_path
        record["page_exported_pages"] = page_exported_pages

    return record


def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    """Атомарно заменить локальный JSONL-отчёт полным результатом запуска."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(f"{canonical_json(record)}\n" for record in records).encode(
        "utf-8"
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )

    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_name, path)

    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _print_commit_result(result: Any) -> None:
    """Вывести счётчики предварительной проверки или записи реестров."""

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
        "Текстовые файлы: "
        f"новых={result.written_blobs}, "
        f"без изменений={result.unchanged_blobs}"
    )


def run_extraction(
    arguments: list[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    schema_dir: Path | None = None,
    extractor: ExtractionFunction = extract_best_text_result,
    page_exporter: PageExportFunction = export_pdf_pages,
    ocr_version_resolver: Callable[[], str] = _tesseract_version,
) -> int:
    """Извлечь пакет PDF, зарегистрировать тексты и записать отчёт."""

    root = project_root.resolve()
    options = _build_parser(root).parse_args(arguments)
    extraction_version = normalize_extraction_version(
        options.extraction_version
    )
    input_path = _project_path(options.input, project_root=root).resolve()
    manifest_dir = _project_path(options.manifest_dir, project_root=root).resolve()
    schema_directory = (
        schema_dir.resolve()
        if schema_dir is not None
        else root / "manifests" / "schemas"
    )
    report_path = (
        _project_path(options.report, project_root=root).resolve()
        if options.report is not None
        else _default_report_path(input_path, manifest_dir)
    )
    page_output_root = _project_path(
        options.page_output_dir,
        project_root=root,
    ).resolve()
    registrations = read_local_file_registrations(input_path)

    if options.limit is not None:
        registrations = registrations[: options.limit]

    _validate_unique_registrations(registrations, project_root=root)

    store = ManifestStore(
        project_root=root,
        manifest_dir=manifest_dir,
        schema_dir=schema_directory,
    )
    expected_snapshot_hashes = store.snapshot_hashes()
    known_artifacts = store.records("artifacts")
    extracted_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    plans: list[ManifestPlan] = []
    report_records: list[dict[str, Any]] = []
    failed_count = 0
    unreadable_count = 0

    for record_index, registration in enumerate(registrations, start=1):
        print(
            f"[{record_index}/{len(registrations)}] "
            f"{Path(registration.relative_path).name}"
        )
        parent_artifact: dict[str, Any] | None = None
        text = ""
        extraction_method: str | None = None
        ocr_attempted = False
        ocr_version: str | None = None
        automatic_readability: bool | None = None
        page_export_manifest_path: str | None = None
        page_exported_pages: int | None = None

        try:
            parent_artifact = find_registered_pdf_artifact(
                store,
                registration.relative_path,
                project_root=root,
            )
            pdf_path = root / parent_artifact["path"]
            extraction_value = extractor(
                pdf_path,
                text_dir=None,
                try_ocr=not options.no_ocr,
            )
            (
                text,
                extraction_method,
                readable,
                page_extraction,
            ) = _unpack_extraction(extraction_value)
            automatic_readability = readable
            ocr_attempted = extraction_method == "pdf_ocr_layout" or (
                extraction_method == "pdf_unreadable" and not options.no_ocr
            )
            ocr_version = (
                ocr_version_resolver()
                if ocr_attempted
                else None
            )

            if options.export_pages:
                if page_extraction is None:
                    raise RuntimeError(
                        "Постраничный экспорт требует полного результата "
                        "PdfTextExtraction"
                    )

                if options.dry_run:
                    with tempfile.TemporaryDirectory(
                        prefix="ruphysbert-page-export-"
                    ) as temporary_directory:
                        page_export = _export_pages_for_pdf(
                            pdf_path,
                            page_extraction,
                            parent_artifact,
                            page_output_root=Path(temporary_directory),
                            extraction_version=extraction_version,
                            page_exporter=page_exporter,
                        )

                    export_description = "проверен без записи"

                else:
                    page_export = _export_pages_for_pdf(
                        pdf_path,
                        page_extraction,
                        parent_artifact,
                        page_output_root=page_output_root,
                        extraction_version=extraction_version,
                        page_exporter=page_exporter,
                    )
                    page_export_manifest_path = _path_for_report(
                        page_export.manifest_path,
                        project_root=root,
                    )
                    export_description = page_export_manifest_path

                page_exported_pages = len(page_export.pages)
                print(
                    "  постраничный экспорт: "
                    f"{page_exported_pages} стр. → "
                    f"{export_description}"
                )

            if not readable:
                unreadable_count += 1
                report_records.append(
                    _report_record(
                        registration,
                        extracted_at=extracted_at,
                        status="not_registered_unreadable",
                        parent_artifact=parent_artifact,
                        text=text,
                        extraction_method=extraction_method,
                        extraction_version=extraction_version,
                        ocr_attempted=ocr_attempted,
                        ocr_version=ocr_version,
                        automatic_readability=False,
                        page_export_manifest_path=page_export_manifest_path,
                        page_exported_pages=page_exported_pages,
                    )
                )
                print(
                    "  не зарегистрирован: автоматическая проверка "
                    f"не пройдена [{extraction_method}, {len(text)} симв.]"
                )
                continue

            plan = plan_extracted_text(
                parent_artifact,
                text,
                extraction_method=extraction_method,
                extraction_version=extraction_version,
                extracted_at=extracted_at,
                existing_artifacts=known_artifacts,
                ocr_version=ocr_version,
            )
            text_artifact = plan.artifacts[0]
            reused = any(
                artifact.get("artifact_id") == text_artifact["artifact_id"]
                for artifact in known_artifacts
            )
            plans.append(plan)
            _remember_artifact(known_artifacts, text_artifact)
            report_records.append(
                _report_record(
                    registration,
                    extracted_at=extracted_at,
                    status="reused" if reused else "registered",
                    parent_artifact=parent_artifact,
                    text=text,
                    extraction_method=extraction_method,
                    extraction_version=extraction_version,
                    ocr_attempted=ocr_attempted,
                    ocr_version=ocr_version,
                    automatic_readability=True,
                    text_artifact=text_artifact,
                    reused=reused,
                    page_export_manifest_path=page_export_manifest_path,
                    page_exported_pages=page_exported_pages,
                )
            )
            print(
                f"  {'повторно использован' if reused else 'подготовлен'}: "
                f"{extraction_method}, {len(text)} симв."
            )

        except EXPECTED_EXTRACTION_ERRORS as exception:
            failed_count += 1
            report_records.append(
                _report_record(
                    registration,
                    extracted_at=extracted_at,
                    status="failed",
                    parent_artifact=parent_artifact,
                    text=text,
                    extraction_method=extraction_method,
                    extraction_version=extraction_version,
                    ocr_attempted=ocr_attempted,
                    ocr_version=ocr_version,
                    automatic_readability=automatic_readability,
                    page_export_manifest_path=page_export_manifest_path,
                    page_exported_pages=page_exported_pages,
                    exception=exception,
                )
            )
            print(f"  ошибка: {exception}", file=sys.stderr)

    if plans:
        result = store.commit(
            _combine_plans(plans),
            dry_run=options.dry_run,
            expected_snapshot_hashes=expected_snapshot_hashes,
        )
        _print_commit_result(result)

    else:
        print("Нет текстов, подготовленных для регистрации.")

    if options.dry_run:
        print("Отчёт не записан: включён режим --dry-run.")

    else:
        _write_jsonl_atomic(report_path, report_records)
        print(f"Отчёт: {report_path}")

    print(
        "Итого: "
        f"карточек={len(registrations)}, "
        f"текстов={len(plans)}, "
        f"нечитаемых={unreadable_count}, "
        f"ошибок={failed_count}"
    )

    return 1 if failed_count else 0


def main() -> int:
    """Запустить обработку аргументов командной строки."""

    return run_extraction()


if __name__ == "__main__":
    raise SystemExit(main())
