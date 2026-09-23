"""Подготовка неизменяемого инженерного запуска проверки OCR."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..collect.pdf_text import (
    COLUMN_GUTTER_PT,
    COLUMN_SPLIT_RATIO,
    LAYOUT_SCAN_DPI,
    MIN_HEADER_PT,
    OCR_DPI,
    SPLIT_MARGIN_PT,
    PdfPageRender,
    PdfPageText,
    extract_selected_pages_from_pdf,
    extract_selected_pages_from_pdf_ocr,
    render_selected_pdf_pages,
)

from .manifests import ManifestStore, canonical_json, sha256_file

RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
PAGE_PLAN_SCHEMA = "ocr_qa_page_plan.schema.json"
RUN_SCHEMA = "ocr_qa_run.schema.json"
FRAME_SCHEMA = "ocr_qa_frame.schema.json"
CANDIDATE_SCHEMA = "ocr_qa_candidate.schema.json"
SEAL_SCHEMA = "ocr_qa_seal.schema.json"
EXPECTED_EXTRACTION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class OcrQaPreparationResult:
    """Пути и счётчики подготовленного запуска OCR QA."""

    qa_run_id: str
    run_path: Path
    frame_path: Path
    candidate_manifest_path: Path
    seal_path: Path
    run_sha256: str
    page_count: int
    variant_count: int
    succeeded_candidates: int
    failed_candidates: int


@dataclass(frozen=True, slots=True)
class _Variant:
    """Один воспроизводимый вариант извлечения текста страницы."""

    variant_id: str
    extraction_method: str
    extraction_version: str
    language: str | None
    config: dict[str, Any]
    run_record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PreparedPage:
    """Выбранная страница с исходным артефактом и идентификаторами."""

    plan: dict[str, Any]
    artifact: dict[str, Any]
    page_index: int
    page_number: int
    page_sample_id: str


PageExtractor = Callable[[Path, tuple[int, ...]], tuple[PdfPageText, ...]]


def prepare_ocr_qa_run(
    *,
    project_root: Path,
    qa_run_id: str,
    page_plan_path: Path,
    reviewer_id: str,
    created_at: str | None = None,
) -> OcrQaPreparationResult:
    """Создать кадр, PNG, кандидаты и паспорт запуска OCR QA."""

    root = Path(project_root).resolve()
    run_id = _validate_run_id(qa_run_id)
    reviewer = _required_string(reviewer_id, field_name="reviewer_id")
    timestamp = _normalize_timestamp(created_at)
    plan_path = _resolve_project_file(
        page_plan_path,
        project_root=root,
        field_name="page_plan_path",
    )
    schema_dir = root / "manifests" / "schemas"
    page_plan = _read_json_object(plan_path)
    _validate_records(
        schema_dir / PAGE_PLAN_SCHEMA,
        [page_plan],
        label="план страниц",
    )

    manifest_output = root / "manifests" / "ocr_qa" / run_id
    data_output = root / "data" / "qa" / "ocr" / run_id
    seal_output = root / "manifests" / "seals" / f"{run_id}.json"
    _require_unused_output(manifest_output, data_output, seal_output)

    artifacts = _selected_artifacts(
        project_root=root,
        page_plan=page_plan,
    )
    prepared_pages = _prepared_pages(page_plan, artifacts)
    variants = _variants(
        project_root=root,
        snapshot_root=manifest_output / "implementation",
    )
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    data_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_staging = Path(
        tempfile.mkdtemp(
            prefix=f".{run_id}.",
            suffix=".staging",
            dir=manifest_output.parent,
        )
    )
    data_staging = Path(
        tempfile.mkdtemp(
            prefix=f".{run_id}.",
            suffix=".staging",
            dir=data_output.parent,
        )
    )

    try:
        result = _build_staging_run(
            project_root=root,
            qa_run_id=run_id,
            page_plan=page_plan,
            page_plan_path=plan_path,
            reviewer_id=reviewer,
            created_at=timestamp,
            artifacts=artifacts,
            prepared_pages=prepared_pages,
            variants=variants,
            manifest_staging=manifest_staging,
            data_staging=data_staging,
            manifest_output=manifest_output,
            data_output=data_output,
            seal_output=seal_output,
            schema_dir=schema_dir,
        )
        data_staging.rename(data_output)

        try:
            manifest_staging.rename(manifest_output)
            _publish_new_file(
                manifest_output / "ocr_qa_seal.json",
                seal_output,
            )

        except OSError:
            shutil.rmtree(data_output, ignore_errors=True)
            shutil.rmtree(manifest_output, ignore_errors=True)
            raise

    finally:
        shutil.rmtree(manifest_staging, ignore_errors=True)
        shutil.rmtree(data_staging, ignore_errors=True)

    return result


def _build_staging_run(
    *,
    project_root: Path,
    qa_run_id: str,
    page_plan: dict[str, Any],
    page_plan_path: Path,
    reviewer_id: str,
    created_at: str,
    artifacts: list[dict[str, Any]],
    prepared_pages: list[_PreparedPage],
    variants: tuple[_Variant, ...],
    manifest_staging: Path,
    data_staging: Path,
    manifest_output: Path,
    data_output: Path,
    seal_output: Path,
    schema_dir: Path,
) -> OcrQaPreparationResult:
    """Полностью собрать и проверить оба временных каталога."""

    source_manifest_path = manifest_output / "source_artifacts.jsonl"
    source_manifest_data = _jsonl_bytes(
        sorted(artifacts, key=lambda artifact: artifact["path"])
    )
    (manifest_staging / "source_artifacts.jsonl").write_bytes(
        source_manifest_data
    )

    copied_plan_data = page_plan_path.read_bytes()
    (manifest_staging / "page_plan.json").write_bytes(copied_plan_data)
    guide_data = _annotation_guide(project_root)
    (manifest_staging / "annotation_guide.md").write_bytes(guide_data)
    _write_implementation_snapshot(
        project_root=project_root,
        variants=variants,
        manifest_staging=manifest_staging,
        manifest_output=manifest_output,
    )

    config_dir = manifest_staging / "config"
    config_dir.mkdir()
    variant_records: list[dict[str, Any]] = []

    for variant in variants:
        config_data = _pretty_json_bytes(variant.config)
        config_name = f"{variant.variant_id}.json"
        (config_dir / config_name).write_bytes(config_data)
        variant_record = dict(variant.run_record)
        variant_record["configuration_path"] = _project_path(
            manifest_output / "config" / config_name,
            project_root=project_root,
        )
        variant_record["configuration_sha256"] = _sha256_bytes(config_data)
        variant_records.append(variant_record)

    frame, render_by_sample = _render_frame(
        project_root=project_root,
        qa_run_id=qa_run_id,
        created_at=created_at,
        page_plan=page_plan,
        prepared_pages=prepared_pages,
        data_staging=data_staging,
        data_output=data_output,
    )
    candidates = _extract_candidates(
        project_root=project_root,
        qa_run_id=qa_run_id,
        created_at=created_at,
        page_plan=page_plan,
        prepared_pages=prepared_pages,
        variants=variants,
        data_staging=data_staging,
        data_output=data_output,
    )
    frame_data = _jsonl_bytes(frame)
    candidate_data = _jsonl_bytes(candidates)
    (manifest_staging / "pages.issued.jsonl").write_bytes(frame_data)
    (manifest_staging / "candidates.jsonl").write_bytes(candidate_data)
    _write_review_materials(
        project_root=project_root,
        qa_run_id=qa_run_id,
        prepared_pages=prepared_pages,
        variants=variants,
        render_by_sample=render_by_sample,
        manifest_staging=manifest_staging,
        manifest_output=manifest_output,
        data_staging=data_staging,
        data_output=data_output,
    )
    run = _run_record(
        project_root=project_root,
        qa_run_id=qa_run_id,
        created_at=created_at,
        reviewer_id=reviewer_id,
        page_plan=page_plan,
        variants=variant_records,
        source_manifest_path=source_manifest_path,
        source_manifest_data=source_manifest_data,
        manifest_output=manifest_output,
        frame_data=frame_data,
        candidate_data=candidate_data,
        guide_data=guide_data,
    )
    run_data = _pretty_json_bytes(run)
    (manifest_staging / "ocr_qa_run.json").write_bytes(run_data)
    seal = _seal_record(
        project_root=project_root,
        qa_run_id=qa_run_id,
        created_at=created_at,
        run_data=run_data,
        manifest_output=manifest_output,
        page_plan_data=copied_plan_data,
        variants=variants,
    )
    seal_data = _pretty_json_bytes(seal)
    (manifest_staging / "ocr_qa_seal.json").write_bytes(seal_data)
    _write_preparation_note(
        qa_run_id=qa_run_id,
        page_plan_path=page_plan_path,
        page_plan_data=copied_plan_data,
        variants=variants,
        prepared_pages=prepared_pages,
        candidates=candidates,
        manifest_staging=manifest_staging,
    )
    _validate_prepared_run(
        project_root=project_root,
        schema_dir=schema_dir,
        manifest_staging=manifest_staging,
        data_staging=data_staging,
        manifest_output=manifest_output,
        data_output=data_output,
        run=run,
        frame=frame,
        candidates=candidates,
        artifacts=artifacts,
        seal=seal,
        run_data=run_data,
    )
    succeeded = sum(
        record["extraction_outcome"] == "succeeded"
        for record in candidates
    )

    return OcrQaPreparationResult(
        qa_run_id=qa_run_id,
        run_path=manifest_output / "ocr_qa_run.json",
        frame_path=manifest_output / "pages.issued.jsonl",
        candidate_manifest_path=manifest_output / "candidates.jsonl",
        seal_path=seal_output,
        run_sha256=seal["run_manifest_sha256"],
        page_count=len(prepared_pages),
        variant_count=len(variants),
        succeeded_candidates=succeeded,
        failed_candidates=len(candidates) - succeeded,
    )


def _validate_run_id(value: str) -> str:
    """Проверить безопасный идентификатор запуска."""

    run_id = _required_string(value, field_name="qa_run_id")

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "qa_run_id может содержать строчные буквы, "
            "цифры, точку, '_' и '-'"
        )

    return run_id


def _required_string(value: Any, *, field_name: str) -> str:
    """Вернуть непустую строку или выдать понятную ошибку."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")

    return value.strip()


def _normalize_timestamp(value: str | None) -> str:
    """Проверить время с часовым поясом или создать текущее."""

    timestamp = datetime.now().astimezone() if value is None else None

    if value is not None:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))

        except ValueError as exception:
            raise ValueError("created_at должен иметь формат ISO 8601") from exception

    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("created_at должен содержать часовой пояс")

    return timestamp.isoformat(timespec="seconds")


def _resolve_project_file(
    path: Path,
    *,
    project_root: Path,
    field_name: str,
) -> Path:
    """Разрешить существующий файл только внутри проекта."""

    candidate = path if path.is_absolute() else project_root / path
    resolved = candidate.resolve()

    if not resolved.is_relative_to(project_root):
        raise ValueError(f"{field_name} должен находиться внутри проекта")

    if not resolved.is_file():
        raise ValueError(f"Файл {field_name} не найден: {resolved}")

    return resolved


def _read_json_object(path: Path) -> dict[str, Any]:
    """Прочитать один JSON-объект из файла."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))

    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ValueError(f"Не удалось прочитать JSON {path}: {exception}") from exception

    if not isinstance(value, dict):
        raise ValueError(f"Файл {path} должен содержать JSON-объект")

    return value


def _validate_records(
    schema_path: Path,
    records: list[dict[str, Any]],
    *,
    label: str,
) -> None:
    """Проверить список записей по JSON Schema Draft 2020-12."""

    try:
        from jsonschema import Draft202012Validator, FormatChecker

    except ImportError as exception:
        raise RuntimeError("Для проверки OCR QA необходим jsonschema") from exception

    schema = _read_json_object(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    for record_index, record in enumerate(records, start=1):
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )

        if not errors:
            continue

        details = []

        for error in errors[:8]:
            location = ".".join(
                str(part) for part in error.absolute_path
            ) or "<root>"
            details.append(f"{location}: {error.message}")

        raise ValueError(
            f"Не прошла схему {label}, запись {record_index}: "
            + "; ".join(details)
        )


def _require_unused_output(
    manifest_output: Path,
    data_output: Path,
    seal_output: Path,
) -> None:
    """Не допустить перезапись уже выданного запуска."""

    occupied = [
        path
        for path in (manifest_output, data_output, seal_output)
        if path.exists() or path.is_symlink()
    ]

    if occupied:
        paths = ", ".join(str(path) for path in occupied)
        raise ValueError(
            "qa_run_id уже использован; смените его, "
            f"чтобы не изменять выданные данные: {paths}"
        )


def _selected_artifacts(
    *,
    project_root: Path,
    page_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Найти в реестре все PDF, упомянутые в плане страниц."""

    store = ManifestStore(project_root=project_root)
    all_artifacts = store.records("artifacts")
    artifacts_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for artifact in all_artifacts:
        if artifact.get("representation") == "pdf" and isinstance(
            artifact.get("path"), str
        ):
            artifacts_by_path[artifact["path"]].append(artifact)

    requested_paths = list(
        dict.fromkeys(page["source_pdf_path"] for page in page_plan["pages"])
    )
    selected: list[dict[str, Any]] = []

    for relative_path in requested_paths:
        matches = artifacts_by_path.get(relative_path, [])

        if len(matches) != 1:
            raise ValueError(
                f"Для PDF {relative_path!r} найдено записей в реестре: "
                f"{len(matches)}"
            )

        artifact = matches[0]
        file_path = _resolve_project_file(
            Path(relative_path),
            project_root=project_root,
            field_name="source_pdf_path",
        )
        digest = sha256_file(file_path)

        if artifact.get("sha256") != digest:
            raise ValueError(f"SHA-256 PDF {relative_path!r} не совпадает с реестром")

        if artifact.get("artifact_id") != f"sha256:{digest}":
            raise ValueError(f"artifact_id PDF {relative_path!r} не совпадает с байтами")

        selected.append(artifact)

    return selected


def _prepared_pages(
    page_plan: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> list[_PreparedPage]:
    """Связать каждую страницу плана с одним PDF-артефактом."""

    artifacts_by_path = {artifact["path"]: artifact for artifact in artifacts}
    prepared: list[_PreparedPage] = []
    seen_samples: set[str] = set()

    for page in page_plan["pages"]:
        source_path = page["source_pdf_path"]
        page_number = page["page_number"]
        stem = Path(source_path).stem.casefold()
        sample_id = f"page_sample_{stem}_p{page_number:04d}"

        if sample_id in seen_samples:
            raise ValueError(f"Повтор page_sample_id={sample_id!r} в плане")

        seen_samples.add(sample_id)
        prepared.append(
            _PreparedPage(
                plan=page,
                artifact=artifacts_by_path[source_path],
                page_index=page_number - 1,
                page_number=page_number,
                page_sample_id=sample_id,
            )
        )

    return prepared


def _variants(
    *,
    project_root: Path,
    snapshot_root: Path,
) -> tuple[_Variant, ...]:
    """Собрать три заранее объявленных варианта извлечения."""

    pymupdf_version = importlib.metadata.version("pymupdf")
    pillow_version = importlib.metadata.version("pillow")
    pytesseract_version = importlib.metadata.version("pytesseract")
    tesseract_version = _tesseract_version()
    language_versions = _tessdata_versions(("rus", "eng"))
    implementation_files = _implementation_files(
        project_root,
        snapshot_root=snapshot_root,
    )
    common_software = {
        "pymupdf": pymupdf_version,
        "pillow": pillow_version,
        "pytesseract": pytesseract_version,
    }
    empty_parameters = {
        "ocr_dpi": None,
        "page_segmentation_mode": None,
        "layout_scan_dpi": None,
        "column_gutter_pt": None,
        "column_split_ratio": None,
        "split_margin_pt": None,
        "minimum_header_pt": None,
    }
    ocr_parameters = {
        "ocr_dpi": OCR_DPI,
        "page_segmentation_mode": 6,
        "layout_scan_dpi": LAYOUT_SCAN_DPI,
        "column_gutter_pt": COLUMN_GUTTER_PT,
        "column_split_ratio": COLUMN_SPLIT_RATIO,
        "split_margin_pt": SPLIT_MARGIN_PT,
        "minimum_header_pt": MIN_HEADER_PT,
    }

    def build_variant(
        *,
        variant_id: str,
        method: str,
        version: str,
        language: str | None,
    ) -> _Variant:
        """Собрать конфигурацию и проекцию одного варианта."""

        languages = [] if language is None else language.split("+")
        parameters = empty_parameters if language is None else ocr_parameters
        language_data = [
            {"language": item, "version": language_versions[item]}
            for item in languages
        ]
        config = {
            "schema_version": "ocr-qa-variant-config-v1",
            "variant_id": variant_id,
            "extraction_method": method,
            "extraction_version": version,
            "language": language,
            "software": {
                **common_software,
                "tesseract": tesseract_version if language else None,
            },
            "language_data_versions": language_data,
            "parameters": parameters,
            "implementation_files": implementation_files,
        }
        run_record = {
            "variant_id": variant_id,
            "extraction_method": method,
            "extraction_version": version,
            "ocr_method": "tesseract" if language else None,
            "ocr_version": tesseract_version if language else None,
            "tessdata_version": (
                _aggregate_tessdata_version(language_data)
                if language
                else None
            ),
            "language_data_versions": language_data,
            "configuration_path": None,
            "configuration_sha256": None,
            "parameters": parameters,
        }

        return _Variant(
            variant_id=variant_id,
            extraction_method=method,
            extraction_version=version,
            language=language,
            config=config,
            run_record=run_record,
        )

    return (
        build_variant(
            variant_id="variant_a",
            method="pdf",
            version=f"pymupdf-text-{pymupdf_version}",
            language=None,
        ),
        build_variant(
            variant_id="variant_b",
            method="pdf_ocr_layout",
            version=f"tesseract-layout-rus-{OCR_DPI}-v1",
            language="rus",
        ),
        build_variant(
            variant_id="variant_c",
            method="pdf_ocr_layout",
            version=f"tesseract-layout-rus-eng-{OCR_DPI}-v1",
            language="rus+eng",
        ),
    )


def _tesseract_version() -> str:
    """Прочитать версию доступного Tesseract."""

    try:
        process = subprocess.run(
            ["tesseract", "--version"],
            check=True,
            capture_output=True,
            text=True,
        )

    except (OSError, subprocess.CalledProcessError) as exception:
        raise RuntimeError("Не удалось определить версию Tesseract") from exception

    first_line = process.stdout.splitlines()[0] if process.stdout else ""
    matched = re.fullmatch(r"tesseract\s+(.+)", first_line.strip())

    if not matched:
        raise RuntimeError("Неожиданный ответ tesseract --version")

    return matched.group(1)


def _tessdata_versions(languages: tuple[str, ...]) -> dict[str, str]:
    """Зафиксировать языковые данные Tesseract по SHA-256."""

    try:
        process = subprocess.run(
            ["tesseract", "--list-langs"],
            check=True,
            capture_output=True,
            text=True,
        )

    except (OSError, subprocess.CalledProcessError) as exception:
        raise RuntimeError("Не удалось найти языковые данные Tesseract") from exception

    output = process.stdout
    matched = re.search(r'"([^"]+/tessdata/)"', output)

    if not matched:
        raise RuntimeError("В tesseract --list-langs не найден каталог tessdata")

    tessdata_dir = Path(matched.group(1))
    versions: dict[str, str] = {}

    for language in languages:
        path = tessdata_dir / f"{language}.traineddata"

        if not path.is_file():
            raise RuntimeError(f"Нет языковых данных Tesseract: {path}")

        versions[language] = f"sha256:{sha256_file(path)}"

    return versions


def _aggregate_tessdata_version(
    language_data: list[dict[str, str]],
) -> str:
    """Вычислить общую версию набора языковых данных."""

    data = canonical_json(language_data).encode("utf-8")
    return f"sha256:{_sha256_bytes(data)}"


def _implementation_files(
    project_root: Path,
    *,
    snapshot_root: Path,
) -> list[dict[str, str]]:
    """Зафиксировать байты кода, даже если рабочее дерево ещё не закоммичено."""

    relative_paths = (
        "src/collect/base.py",
        "src/collect/pdf_text.py",
        "src/corpus/manifests.py",
        "src/corpus/schema_validation.py",
        "src/corpus/ocr_qa_preparation.py",
        "scripts/prepare_ocr_qa.py",
    )
    result: list[dict[str, str]] = []

    for relative_path in relative_paths:
        path = project_root / relative_path

        if not path.is_file():
            raise RuntimeError(f"Не найден файл реализации: {relative_path}")

        result.append(
            {
                "path": relative_path,
                "sha256": sha256_file(path),
                "snapshot_path": _project_path(
                    snapshot_root / relative_path,
                    project_root=project_root,
                ),
            }
        )

    return result


def _write_implementation_snapshot(
    *,
    project_root: Path,
    variants: tuple[_Variant, ...],
    manifest_staging: Path,
    manifest_output: Path,
) -> None:
    """Скопировать точные байты реализации внутрь локального запуска."""

    if not variants:
        raise ValueError("Нельзя зафиксировать реализацию без вариантов")

    implementation_files = variants[0].config["implementation_files"]

    for variant in variants[1:]:
        if variant.config["implementation_files"] != implementation_files:
            raise ValueError("Варианты ссылаются на разные снимки реализации")

    for record in implementation_files:
        source_path = project_root / record["path"]
        final_path = project_root / record["snapshot_path"]

        try:
            relative_path = final_path.relative_to(manifest_output)

        except ValueError as exception:
            raise ValueError(
                "Путь снимка реализации находится вне каталога запуска: "
                f"{record['snapshot_path']}"
            ) from exception

        data = source_path.read_bytes()

        if _sha256_bytes(data) != record["sha256"]:
            raise RuntimeError(
                f"Файл реализации изменился во время подготовки: {source_path}"
            )

        staged_path = manifest_staging / relative_path
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(data)


def _render_frame(
    *,
    project_root: Path,
    qa_run_id: str,
    created_at: str,
    page_plan: dict[str, Any],
    prepared_pages: list[_PreparedPage],
    data_staging: Path,
    data_output: Path,
) -> tuple[list[dict[str, Any]], dict[str, PdfPageRender]]:
    """Растеризовать страницы и собрать зафиксированный кадр."""

    pages_by_pdf = _pages_by_pdf(prepared_pages)
    render_by_sample: dict[str, PdfPageRender] = {}

    for source_path, pages in pages_by_pdf.items():
        pdf_path = project_root / source_path
        page_indices = tuple(page.page_index for page in pages)
        renders = render_selected_pdf_pages(
            pdf_path,
            page_indices,
            dpi=page_plan["render_dpi"],
        )

        for prepared_page, render in zip(pages, renders, strict=True):
            render_by_sample[prepared_page.page_sample_id] = render

    render_dir = data_staging / "renders"
    render_dir.mkdir(parents=True)
    frame: list[dict[str, Any]] = []

    for prepared_page in prepared_pages:
        render = render_by_sample[prepared_page.page_sample_id]
        file_name = f"{prepared_page.page_sample_id}.png"
        (render_dir / file_name).write_bytes(render.png)
        final_render_path = data_output / "renders" / file_name
        frame.append(
            {
                "schema_version": "ocr-qa-frame-v1",
                "frame_record_id": f"ocr_frame_{prepared_page.page_sample_id}",
                "qa_run_id": qa_run_id,
                "page_sample_id": prepared_page.page_sample_id,
                "work_id": prepared_page.artifact["work_id"],
                "source_id": page_plan["source_id"],
                "source_group_id": page_plan["source_group_id"],
                "source_pdf_artifact_id": prepared_page.artifact["artifact_id"],
                "source_pdf_path": prepared_page.artifact["path"],
                "publication_year": page_plan["publication_year"],
                "page_index": prepared_page.page_index,
                "page_number": prepared_page.page_number,
                "printed_page_label": prepared_page.plan["printed_page_label"],
                "page_render_path": _project_path(
                    final_render_path,
                    project_root=project_root,
                ),
                "page_render_sha256": _sha256_bytes(render.png),
                "page_render_dpi": page_plan["render_dpi"],
                "stratum_id": "manual_challenge_only",
                "layout": prepared_page.plan["layout"],
                "scan_quality": prepared_page.plan["scan_quality"],
                "selection_tags": prepared_page.plan["selection_tags"],
                "selection_reason": prepared_page.plan["selection_reason"],
                "selection_role": "manual_challenge",
                "created_at": created_at,
            }
        )

    return frame, render_by_sample


def _extract_candidates(
    *,
    project_root: Path,
    qa_run_id: str,
    created_at: str,
    page_plan: dict[str, Any],
    prepared_pages: list[_PreparedPage],
    variants: tuple[_Variant, ...],
    data_staging: Path,
    data_output: Path,
) -> list[dict[str, Any]]:
    """Извлечь текст всех вариантов и создать их реестр."""

    candidates: list[dict[str, Any]] = []
    pages_by_pdf = _pages_by_pdf(prepared_pages)

    for variant in variants:
        candidate_dir = data_staging / "candidate" / variant.variant_id
        candidate_dir.mkdir(parents=True)

        for source_path, pages in pages_by_pdf.items():
            page_indices = tuple(page.page_index for page in pages)
            extractor = _variant_extractor(variant)

            try:
                extracted_pages = extractor(
                    project_root / source_path,
                    page_indices,
                )

            except EXPECTED_EXTRACTION_ERRORS as exception:
                for prepared_page in pages:
                    candidates.append(
                        _failed_candidate_record(
                            qa_run_id=qa_run_id,
                            created_at=created_at,
                            prepared_page=prepared_page,
                            variant=variant,
                            source_id=page_plan["source_id"],
                            source_group_id=page_plan["source_group_id"],
                            exception=exception,
                        )
                    )

                continue

            if len(extracted_pages) != len(pages):
                raise RuntimeError(
                    f"Вариант {variant.variant_id} вернул "
                    f"{len(extracted_pages)} страниц вместо {len(pages)}"
                )

            for prepared_page, extracted_page in zip(
                pages,
                extracted_pages,
                strict=True,
            ):
                if extracted_page.page_index != prepared_page.page_index:
                    raise RuntimeError(
                        f"Вариант {variant.variant_id} изменил "
                        f"page_index={prepared_page.page_index}"
                    )

                text = _serialized_page_text(extracted_page.text)
                data = text.encode("utf-8")
                file_name = f"{prepared_page.page_sample_id}.txt"
                (candidate_dir / file_name).write_bytes(data)
                final_path = (
                    data_output
                    / "candidate"
                    / variant.variant_id
                    / file_name
                )
                digest = _sha256_bytes(data)
                candidates.append(
                    {
                        "schema_version": "ocr-qa-candidate-v1",
                        "candidate_record_id": (
                            f"candidate_{prepared_page.page_sample_id}_"
                            f"{variant.variant_id}"
                        ),
                        "qa_run_id": qa_run_id,
                        "page_sample_id": prepared_page.page_sample_id,
                        "variant_id": variant.variant_id,
                        "work_id": prepared_page.artifact["work_id"],
                        "source_id": page_plan["source_id"],
                        "source_group_id": page_plan["source_group_id"],
                        "source_pdf_artifact_id": prepared_page.artifact[
                            "artifact_id"
                        ],
                        "candidate_artifact_id": f"sha256:{digest}",
                        "page_index": prepared_page.page_index,
                        "page_number": prepared_page.page_number,
                        "extraction_outcome": "succeeded",
                        "error_code": None,
                        "error_detail": None,
                        "candidate_page_text_path": _project_path(
                            final_path,
                            project_root=project_root,
                        ),
                        "candidate_page_text_sha256": digest,
                        "candidate_page_text_characters": len(text),
                        "created_at": created_at,
                    }
                )

    return candidates


def _variant_extractor(variant: _Variant) -> PageExtractor:
    """Вернуть функцию извлечения для одного варианта."""

    if variant.language is None:
        return extract_selected_pages_from_pdf

    def extract_ocr(
        path: Path,
        page_indices: tuple[int, ...],
    ) -> tuple[PdfPageText, ...]:
        """Распознать выбранные страницы с языком варианта."""

        return extract_selected_pages_from_pdf_ocr(
            path,
            page_indices,
            lang=variant.language or "rus",
            dpi=OCR_DPI,
        )

    return extract_ocr


def _failed_candidate_record(
    *,
    qa_run_id: str,
    created_at: str,
    prepared_page: _PreparedPage,
    variant: _Variant,
    source_id: str,
    source_group_id: str,
    exception: Exception,
) -> dict[str, Any]:
    """Собрать честную запись об ошибке извлечения."""

    return {
        "schema_version": "ocr-qa-candidate-v1",
        "candidate_record_id": (
            f"candidate_{prepared_page.page_sample_id}_{variant.variant_id}"
        ),
        "qa_run_id": qa_run_id,
        "page_sample_id": prepared_page.page_sample_id,
        "variant_id": variant.variant_id,
        "work_id": prepared_page.artifact["work_id"],
        "source_id": source_id,
        "source_group_id": source_group_id,
        "source_pdf_artifact_id": prepared_page.artifact["artifact_id"],
        "candidate_artifact_id": None,
        "page_index": prepared_page.page_index,
        "page_number": prepared_page.page_number,
        "extraction_outcome": "failed",
        "error_code": type(exception).__name__,
        "error_detail": str(exception) or repr(exception),
        "candidate_page_text_path": None,
        "candidate_page_text_sha256": None,
        "candidate_page_text_characters": 0,
        "created_at": created_at,
    }


def _pages_by_pdf(
    prepared_pages: list[_PreparedPage],
) -> dict[str, list[_PreparedPage]]:
    """Сгруппировать страницы по PDF с сохранением порядка."""

    result: dict[str, list[_PreparedPage]] = defaultdict(list)

    for prepared_page in prepared_pages:
        result[prepared_page.artifact["path"]].append(prepared_page)

    return dict(result)


def _serialized_page_text(text: str) -> str:
    """Привести текст страницы к UTF-8, NFC и окончаниям LF."""

    with_lf = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", with_lf).strip()


def _run_record(
    *,
    project_root: Path,
    qa_run_id: str,
    created_at: str,
    reviewer_id: str,
    page_plan: dict[str, Any],
    variants: list[dict[str, Any]],
    source_manifest_path: Path,
    source_manifest_data: bytes,
    manifest_output: Path,
    frame_data: bytes,
    candidate_data: bytes,
    guide_data: bytes,
) -> dict[str, Any]:
    """Собрать неизменяемый паспорт инженерного запуска."""

    return {
        "schema_version": "ocr-qa-run-v1",
        "qa_run_id": qa_run_id,
        "run_kind": "engineering_pilot",
        "run_status": "prepared",
        "protocol_version": page_plan["protocol_version"],
        "source_artifacts_manifest_path": _project_path(
            source_manifest_path,
            project_root=project_root,
        ),
        "source_artifacts_schema_id": "urn:ruphysbert:schema:artifacts:v1",
        "source_artifacts_manifest_sha256": _sha256_bytes(
            source_manifest_data
        ),
        "sample_frame_path": _project_path(
            manifest_output / "pages.issued.jsonl",
            project_root=project_root,
        ),
        "sample_frame_sha256": _sha256_bytes(frame_data),
        "candidate_manifest_path": _project_path(
            manifest_output / "candidates.jsonl",
            project_root=project_root,
        ),
        "candidate_manifest_sha256": _sha256_bytes(candidate_data),
        "annotation_guide_path": _project_path(
            manifest_output / "annotation_guide.md",
            project_root=project_root,
        ),
        "annotation_guide_sha256": _sha256_bytes(guide_data),
        "code_commit": _git_commit(project_root),
        "selection_plan": {
            "method": "manual_challenge_only",
            "selection_seed": 0,
            "target_pdf_count": 0,
            "target_page_count": 0,
            "target_manual_challenge_page_count": len(page_plan["pages"]),
            "minimum_prose_characters": page_plan[
                "minimum_prose_characters"
            ],
            "stratification_dimensions": [
                "source_id",
                "publication_year",
                "scan_quality",
                "layout",
            ],
            "strata": [],
        },
        "variants": variants,
        "comparison_plan": {
            "text_serialization": "utf8-nfc-lf-v1",
            "prose_normalization_version": "ocr-prose-norm-v1",
            "metric_version": "ocr-metrics-v1",
            "cer_unit": "unicode_code_point",
            "wer_tokenization": "whitespace_after_normalization",
            "excluded_from_prose_metrics": [
                "bibliography",
                "formula",
                "non_russian_text",
                "table",
                "figure_caption",
            ],
        },
        "confidence_interval_plan": {
            "method": "hierarchical_cluster_bootstrap",
            "confidence_level": 0.95,
            "side": "one_sided",
            "cluster_units": ["work_id", "page_sample_id"],
            "replicates": 10000,
            "bootstrap_seed": 20260904,
            "quantile_method": "percentile",
        },
        "acceptance_thresholds": {
            "maximum_cer_upper_bound": 0.03,
            "maximum_wer_upper_bound": 0.1,
            "maximum_critical_reading_order_rate_upper_bound": 0.05,
            "minimum_formula_detection_f1_lower_bound": 0.9,
            "maximum_critical_formula_damage_rate_upper_bound": 0.05,
            "minimum_group_pages": 10,
            "minimum_group_prose_characters": 2000,
            "minimum_formula_occurrences": 200,
            "minimum_formula_work_ids": 30,
        },
        "review_plan": {
            "reviewer_ids": [reviewer_id],
            "gold_verification_required": True,
            "variant_labels_blinded": True,
            "reviewer_materials_exclude_variant_configuration": True,
        },
        "supersedes_run_id": None,
        "created_at": created_at,
    }


def _seal_record(
    *,
    project_root: Path,
    qa_run_id: str,
    created_at: str,
    run_data: bytes,
    manifest_output: Path,
    page_plan_data: bytes,
    variants: tuple[_Variant, ...],
) -> dict[str, Any]:
    """Собрать внешний отпечаток паспорта и снимка реализации."""

    implementation_files = variants[0].config["implementation_files"]

    return {
        "schema_version": "ocr-qa-seal-v1",
        "qa_run_id": qa_run_id,
        "run_manifest_path": _project_path(
            manifest_output / "ocr_qa_run.json",
            project_root=project_root,
        ),
        "run_manifest_sha256": _sha256_bytes(run_data),
        "page_plan_path": _project_path(
            manifest_output / "page_plan.json",
            project_root=project_root,
        ),
        "page_plan_sha256": _sha256_bytes(page_plan_data),
        "implementation_files": implementation_files,
        "created_at": created_at,
    }


def _annotation_guide(project_root: Path) -> bytes:
    """Собрать локальную копию протокола и бланка страницы."""

    protocol = (project_root / "docs" / "OCR_QA_PROTOCOL.md").read_text(
        encoding="utf-8"
    )
    template = (
        project_root / "docs" / "templates" / "OCR_QA_PAGE_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    combined = (
        "# Зафиксированное руководство OCR QA\n\n"
        "Ниже побайтно зафиксированы протокол и бланк, "
        "действовавшие при выдаче запуска.\n\n"
        "---\n\n"
        f"{protocol.rstrip()}\n\n"
        "---\n\n"
        f"{template.rstrip()}\n"
    )
    return combined.encode("utf-8")


def _write_review_materials(
    *,
    project_root: Path,
    qa_run_id: str,
    prepared_pages: list[_PreparedPage],
    variants: tuple[_Variant, ...],
    render_by_sample: dict[str, PdfPageRender],
    manifest_staging: Path,
    manifest_output: Path,
    data_staging: Path,
    data_output: Path,
) -> None:
    """Создать очередь, пустые эталоны и бланки для человека."""

    gold_dir = data_staging / "gold" / "prose"
    candidate_prose_root = data_staging / "candidate_prose"
    notes_dir = data_staging / "notes"
    gold_dir.mkdir(parents=True)
    candidate_prose_root.mkdir(parents=True)
    notes_dir.mkdir(parents=True)

    for variant in variants:
        (candidate_prose_root / variant.variant_id).mkdir()

    first_pass_ids = _first_pass_sample_ids(prepared_pages)
    queue_lines = [
        f"# Очередь ручной проверки {qa_run_id}",
        "",
        f"Сначала заполните страницы первого прохода ({len(first_pass_ids)}). "
        "До этого не открывайте каталоги `candidate/` и `config/`.",
        "",
        "## Первый проход",
        "",
    ]

    for sample_id in first_pass_ids:
        queue_lines.append(f"- [ ] `{sample_id}`")

    queue_lines.extend(["", "## Остальные страницы", ""])

    for prepared_page in prepared_pages:
        if prepared_page.page_sample_id in first_pass_ids:
            continue

        queue_lines.append(f"- [ ] `{prepared_page.page_sample_id}`")

    queue_lines.extend(
        [
            "",
            "## Порядок для каждой страницы",
            "",
            "1. Откройте PNG и бланк из `notes/`.",
            "2. По изображению выберите области русской прозы и их порядок.",
            "3. Наберите эталон в `gold/prose/`, не исправляя опечатки статьи.",
            "4. Только после эталона откройте тексты `variant_a`, `variant_b`, `variant_c`.",
            "5. Скопируйте из каждого кандидата только те же области в "
            "`candidate_prose/`, не исправляя текст.",
            "6. Заполните ошибки порядка чтения и заметки о формулах.",
            "7. При одном проверяющем повторно сверьте эталон не ранее чем через сутки.",
            "",
            "Это калибровка. Её метрики не дают допуск всему корпусу.",
            "",
        ]
    )
    (manifest_staging / "REVIEW_QUEUE.md").write_text(
        "\n".join(queue_lines),
        encoding="utf-8",
    )

    for prepared_page in prepared_pages:
        sample_id = prepared_page.page_sample_id
        gold_path = gold_dir / f"{sample_id}.txt"
        gold_path.write_bytes(b"")
        render = render_by_sample[sample_id]
        final_gold_path = data_output / "gold" / "prose" / gold_path.name
        final_render_path = data_output / "renders" / f"{sample_id}.png"
        candidate_paths: list[tuple[Path, Path]] = []

        for variant in variants:
            candidate_page_path = (
                data_output
                / "candidate"
                / variant.variant_id
                / f"{sample_id}.txt"
            )
            candidate_prose_path = (
                data_output
                / "candidate_prose"
                / variant.variant_id
                / f"{sample_id}.txt"
            )
            staged_prose_path = (
                candidate_prose_root
                / variant.variant_id
                / f"{sample_id}.txt"
            )
            staged_prose_path.write_bytes(b"")
            candidate_paths.append(
                (candidate_page_path, candidate_prose_path)
            )
        note = _page_note(
            project_root=project_root,
            prepared_page=prepared_page,
            render=render,
            render_path=final_render_path,
            gold_path=final_gold_path,
            candidate_paths=candidate_paths,
        )
        (notes_dir / f"{sample_id}.md").write_text(note, encoding="utf-8")

    reviewer_note = (
        f"# Материалы проверяющего {qa_run_id}\n\n"
        "Очередь: "
        f"`{_project_path(manifest_output / 'REVIEW_QUEUE.md', project_root=project_root)}`.\n\n"
        "Конфигурации вариантов сознательно не включены. Сначала "
        "подготавливается эталон по PNG, затем открываются кандидаты.\n"
    )
    (data_staging / "README.md").write_text(reviewer_note, encoding="utf-8")


def _first_pass_sample_ids(
    prepared_pages: list[_PreparedPage],
) -> tuple[str, ...]:
    """Выбрать объявленные либо первые пять страниц для проверки формы."""

    selected = tuple(
        page.page_sample_id
        for page in prepared_pages
        if page.plan.get("first_pass") is True
    )

    if selected:
        return selected

    return tuple(
        page.page_sample_id
        for page in prepared_pages[:5]
    )


def _page_note(
    *,
    project_root: Path,
    prepared_page: _PreparedPage,
    render: PdfPageRender,
    render_path: Path,
    gold_path: Path,
    candidate_paths: list[tuple[Path, Path]],
) -> str:
    """Собрать ручной бланк одной страницы."""

    candidate_lines = "\n".join(
        "- Полная страница: "
        f"`{_project_path(page_path, project_root=project_root)}`; "
        "те же области прозы: "
        f"`{_project_path(prose_path, project_root=project_root)}`"
        for page_path, prose_path in candidate_paths
    )
    return (
        f"# {prepared_page.page_sample_id}\n\n"
        f"- PDF: `{prepared_page.artifact['path']}`\n"
        f"- Физическая страница: {prepared_page.page_number}\n"
        f"- Печатный номер: {prepared_page.plan['printed_page_label']}\n"
        f"- PNG: `{_project_path(render_path, project_root=project_root)}`\n"
        f"- Размер PNG: {render.width} × {render.height}\n"
        f"- Эталон прозы: `{_project_path(gold_path, project_root=project_root)}`\n\n"
        "## Области русской прозы\n\n"
        "Для каждой области укажите `region_id`, границы "
        "`x_min, y_min, x_max, y_max` от 0 до 1 и порядок.\n\n"
        "- [ ] `region_01`: \n"
        "- [ ] `region_02`: \n\n"
        "## Ожидаемый порядок блоков\n\n"
        "1. \n\n"
        "## Кандидаты\n\n"
        "Не открывайте до завершения эталона.\n\n"
        f"{candidate_lines}\n\n"
        "В файлы областей прозы копируйте неизменённый текст кандидата "
        "только для областей, включённых в эталон, и в том же порядке.\n\n"
        "## Порядок чтения и формулы\n\n"
        "- `variant_a`: \n"
        "- `variant_b`: \n"
        "- `variant_c`: \n"
        "- Эталонные формулы и обозначения: \n"
        "- Заметки: \n"
    )


def _write_preparation_note(
    *,
    qa_run_id: str,
    page_plan_path: Path,
    page_plan_data: bytes,
    variants: tuple[_Variant, ...],
    prepared_pages: list[_PreparedPage],
    candidates: list[dict[str, Any]],
    manifest_staging: Path,
) -> None:
    """Описать техническую комплектацию запуска для исследователя."""

    succeeded = sum(
        candidate["extraction_outcome"] == "succeeded"
        for candidate in candidates
    )
    lines = [
        f"# Подготовка {qa_run_id}",
        "",
        "Это инженерная калибровка на целенаправленно выбранных "
        "страницах. Она не является окончательным критерием допуска корпуса.",
        "",
        f"- Исходный план: `{page_plan_path}`",
        f"- SHA-256 плана: `{_sha256_bytes(page_plan_data)}`",
        f"- PDF: {len({page.artifact['artifact_id'] for page in prepared_pages})}",
        f"- Страниц: {len(prepared_pages)}",
        f"- Вариантов: {len(variants)}",
        f"- Успешных кандидатов: {succeeded} из {len(candidates)}",
        "",
        "## Раскрытие меток вариантов",
        "",
    ]

    for variant in variants:
        language = variant.language or "встроенный текстовый слой"
        lines.append(
            f"- `{variant.variant_id}`: {variant.extraction_method}, {language}."
        )

    lines.extend(
        [
            "",
            "Проверяющему этот раздел не показывается до завершения эталона.",
            "",
            "Файлы `pages.submitted.jsonl`, `formulas.submitted.jsonl` и "
            "`ocr_qa_summary.json` намеренно не созданы: без ручной "
            "разметки они были бы ложным результатом.",
            "",
        ]
    )
    (manifest_staging / "PREPARATION.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def _validate_prepared_run(
    *,
    project_root: Path,
    schema_dir: Path,
    manifest_staging: Path,
    data_staging: Path,
    manifest_output: Path,
    data_output: Path,
    run: dict[str, Any],
    frame: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    seal: dict[str, Any],
    run_data: bytes,
) -> None:
    """Проверить схемы, связи, файлы и SHA-256 до публикации."""

    _validate_records(schema_dir / RUN_SCHEMA, [run], label="паспорт")
    _validate_records(schema_dir / FRAME_SCHEMA, frame, label="кадр")
    _validate_records(
        schema_dir / CANDIDATE_SCHEMA,
        candidates,
        label="кандидаты",
    )
    _validate_records(schema_dir / SEAL_SCHEMA, [seal], label="печать")

    if seal["run_manifest_sha256"] != _sha256_bytes(run_data):
        raise ValueError("Печать не совпадает с байтами паспорта запуска")
    expected_candidate_count = len(frame) * len(run["variants"])

    if len(candidates) != expected_candidate_count:
        raise ValueError(
            f"Ожидалось кандидатов {expected_candidate_count}, "
            f"получено {len(candidates)}"
        )

    frame_by_sample = {record["page_sample_id"]: record for record in frame}
    variant_ids = {variant["variant_id"] for variant in run["variants"]}
    candidate_keys: set[tuple[str, str]] = set()

    for candidate in candidates:
        key = (candidate["page_sample_id"], candidate["variant_id"])

        if key in candidate_keys:
            raise ValueError(f"Повторный кандидат {key!r}")

        candidate_keys.add(key)
        frame_record = frame_by_sample.get(candidate["page_sample_id"])

        if frame_record is None or candidate["variant_id"] not in variant_ids:
            raise ValueError(f"Кандидат {key!r} не связан с паспортом")

        for field_name in (
            "work_id",
            "source_id",
            "source_group_id",
            "source_pdf_artifact_id",
            "page_index",
            "page_number",
        ):
            if candidate[field_name] != frame_record[field_name]:
                raise ValueError(
                    f"Кандидат {key!r} изменил {field_name} кадра"
                )

    _check_staged_hash(
        run["source_artifacts_manifest_path"],
        run["source_artifacts_manifest_sha256"],
        project_root=project_root,
        final_root=manifest_output,
        staging_root=manifest_staging,
    )
    _check_staged_hash(
        run["sample_frame_path"],
        run["sample_frame_sha256"],
        project_root=project_root,
        final_root=manifest_output,
        staging_root=manifest_staging,
    )
    _check_staged_hash(
        run["candidate_manifest_path"],
        run["candidate_manifest_sha256"],
        project_root=project_root,
        final_root=manifest_output,
        staging_root=manifest_staging,
    )
    _check_staged_hash(
        run["annotation_guide_path"],
        run["annotation_guide_sha256"],
        project_root=project_root,
        final_root=manifest_output,
        staging_root=manifest_staging,
    )

    for variant in run["variants"]:
        _check_staged_hash(
            variant["configuration_path"],
            variant["configuration_sha256"],
            project_root=project_root,
            final_root=manifest_output,
            staging_root=manifest_staging,
        )

    for implementation_file in seal["implementation_files"]:
        _check_staged_hash(
            implementation_file["snapshot_path"],
            implementation_file["sha256"],
            project_root=project_root,
            final_root=manifest_output,
            staging_root=manifest_staging,
        )

    for frame_record in frame:
        _check_staged_hash(
            frame_record["page_render_path"],
            frame_record["page_render_sha256"],
            project_root=project_root,
            final_root=data_output,
            staging_root=data_staging,
        )

    for candidate in candidates:
        if candidate["extraction_outcome"] != "succeeded":
            continue

        _check_staged_hash(
            candidate["candidate_page_text_path"],
            candidate["candidate_page_text_sha256"],
            project_root=project_root,
            final_root=data_output,
            staging_root=data_staging,
        )

    source_ids = {artifact["artifact_id"] for artifact in artifacts}
    frame_source_ids = {record["source_pdf_artifact_id"] for record in frame}

    if source_ids != frame_source_ids:
        raise ValueError("Снимок PDF-артефактов не совпадает с кадром")


def _check_staged_hash(
    relative_path: str,
    expected_sha256: str,
    *,
    project_root: Path,
    final_root: Path,
    staging_root: Path,
) -> None:
    """Проверить хеш файла в его временном каталоге."""

    final_path = project_root / relative_path

    try:
        suffix = final_path.relative_to(final_root)

    except ValueError as exception:
        raise ValueError(
            f"Ссылочный путь вне каталога запуска: {relative_path}"
        ) from exception

    staged_path = staging_root / suffix

    if not staged_path.is_file():
        raise ValueError(f"Не найден подготовленный файл: {relative_path}")

    if sha256_file(staged_path) != expected_sha256:
        raise ValueError(f"Не совпадает SHA-256 файла: {relative_path}")


def _git_commit(project_root: Path) -> str:
    """Получить базовую ревизию Git для паспорта."""

    try:
        process = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )

    except (OSError, subprocess.CalledProcessError) as exception:
        raise RuntimeError("Не удалось определить Git-ревизию") from exception

    commit = process.stdout.strip()

    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("git rev-parse HEAD вернул некорректную ревизию")

    return commit


def _project_path(path: Path, *, project_root: Path) -> str:
    """Преобразовать путь в ссылку от корня проекта."""

    resolved = path.resolve()

    if not resolved.is_relative_to(project_root):
        raise ValueError(f"Путь находится вне проекта: {path}")

    return resolved.relative_to(project_root).as_posix()


def _publish_new_file(source_path: Path, output_path: Path) -> None:
    """Опубликовать новый файл без перезаписи существующего."""

    data = source_path.read_bytes()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    created = False

    try:
        with output_path.open("xb") as output_file:
            created = True
            output_file.write(data)

    except OSError:
        if created:
            output_path.unlink(missing_ok=True)

        raise


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    """Сериализовать записи в канонический JSONL."""

    return "".join(
        f"{canonical_json(record)}\n"
        for record in records
    ).encode("utf-8")


def _pretty_json_bytes(value: dict[str, Any]) -> bytes:
    """Сериализовать JSON в читаемом и устойчивом виде."""

    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return f"{data}\n".encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    """Вычислить шестнадцатеричный SHA-256 переданных байтов."""

    return hashlib.sha256(data).hexdigest()
