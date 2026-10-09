"""Проверка происхождения и безопасная запись кандидатов очищенного текста."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from src.collect.pdf_text import PdfPageText, _combine_page_texts
from src.corpus.manifests import ManifestStore, sha256_file
from src.preprocess.text_cleaning import CLEANING_VERSION, LineExclusion, clean_pages

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_BASE = Path("data/qa/preprocessing")


@dataclass(frozen=True, slots=True)
class PreparedCleaning:
    """Проверенный пакет в памяти без изменений исходников и реестров."""

    project_root: Path
    output_dir: Path
    files: dict[str, bytes]
    report: dict[str, Any]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклонить повторный ключ вместо неявного выбора последнего значения."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторный ключ JSON: {key}")

        result[key] = value

    return result


def _reject_constant(value: str) -> None:
    """Запретить не предусмотренные JSON значения NaN и Infinity."""

    raise ValueError(f"Недопустимая константа JSON: {value}")


def _read_json(data: bytes) -> Any:
    """Прочитать строгий UTF-8 JSON без дубликатов ключей."""

    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _json_bytes(value: Any) -> bytes:
    """Сериализовать отчёт детерминированно, без времени запуска."""

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return (text + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    """Вычислить контрольную сумму точных байтов."""

    return hashlib.sha256(data).hexdigest()


def _checked_path(root: Path, value: str | Path) -> Path:
    """Разрешить путь внутри корня, отклонив обходы и символические ссылки."""

    path = Path(value)

    if ".." in path.parts:
        raise ValueError(f"Недопустимый переход к родительскому каталогу: {value}")

    candidate = path if path.is_absolute() else root / path

    try:
        relative = candidate.relative_to(root)

    except ValueError as exception:
        raise ValueError(f"Путь выходит за пределы {root}: {value}") from exception

    current = root

    for part in relative.parts:
        current = current / part

        if current.is_symlink():
            raise ValueError(f"Символическая ссылка недопустима: {current}")

    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Путь выходит за пределы {root}: {value}")

    return candidate


def _validator(schema_dir: Path, name: str) -> Draft202012Validator:
    """Загрузить локальную схему, не обращаясь к сети."""

    schema = _read_json((schema_dir / name).read_bytes())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate(value: Any, validator: Draft202012Validator, label: str) -> None:
    """Показать первую ошибку схемы с понятным контекстом."""

    error = next(validator.iter_errors(value), None)

    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path)
        raise ValueError(f"{label}: ошибка схемы в {location or '/'}: {error.message}")


def _artifact_bytes(record: dict[str, Any], root: Path) -> bytes:
    """Проверить зарегистрированный файл внутри data по байтам и SHA-256."""

    path = _checked_path(root, record["path"])
    _checked_path(root / "data", path)
    data = path.read_bytes()
    checksum = _sha256(data)

    if (
        checksum != record["sha256"]
        or record["artifact_id"] != f"sha256:{checksum}"
        or len(data) != record["bytes"]
    ):
        raise ValueError(f"Исходный файл не совпадает с реестром: {path}")

    return data


def _read_pages(
    document: dict[str, Any],
    artifact: dict[str, Any],
    parent: dict[str, Any],
    root: Path,
    validator: Draft202012Validator,
) -> tuple[PdfPageText, ...]:
    """Проверить закреплённый индекс, порядок страниц и каждый текст страницы."""

    if Path(document["pages_manifest_path"]).is_absolute():
        raise ValueError("В плане нужен относительный путь индекса страниц")

    manifest_path = _checked_path(root, document["pages_manifest_path"])
    _checked_path(root / "data", manifest_path)
    manifest_data = manifest_path.read_bytes()

    if _sha256(manifest_data) != document["pages_manifest_sha256"]:
        raise ValueError(f"SHA-256 индекса страниц не совпадает: {manifest_path}")

    pages: list[PdfPageText] = []

    for page_index, line in enumerate(manifest_data.splitlines()):
        record = _read_json(line)
        _validate(record, validator, f"{manifest_path}:{page_index + 1}")

        if (
            record["page_index"] != page_index
            or record["page_number"] != page_index + 1
            or record["path"] != f"page_{page_index + 1:04d}.txt"
        ):
            raise ValueError("Индекс страниц должен быть полным и последовательным, начиная с 1")

        if (
            record["source_pdf_path"] != parent["path"]
            or record["source_pdf_sha256"] != parent["sha256"]
            or record["extraction_method"] != artifact["extraction_method"]
            or record["extraction_version"] != artifact["extraction_version"]
        ):
            raise ValueError("Происхождение страниц не совпадает с зарегистрированным текстом")

        page_path = _checked_path(manifest_path.parent, record["path"])
        page_data = page_path.read_bytes()
        text = page_data.decode("utf-8")

        if _sha256(page_data) != record["sha256"] or len(text) != record["characters"]:
            raise ValueError(f"Содержимое страницы не совпадает с индексом: {page_path}")

        pages.append(PdfPageText(page_index, page_index + 1, text))

    if not pages:
        raise ValueError("Индекс страниц пуст")

    return tuple(pages)


def _prepare_document(
    document: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    works: dict[str, dict[str, Any]],
    root: Path,
    validator: Draft202012Validator,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Связать PDF, полный текст и страницы перед применением явных правил."""

    artifact = artifacts.get(document["source_artifact_id"])

    if artifact is None or artifact["work_id"] != document["work_id"]:
        raise ValueError("Исходный текст не зарегистрирован для указанного work_id")

    if (
        artifact["representation"] not in {"plain_text", "ocr_text"}
        or artifact["content_role"] != "full_text"
        or artifact["extraction_method"] not in {"pdf", "pdf_ocr_layout"}
        or artifact.get("preprocessing_version") is not None
    ):
        raise ValueError("Для очистки нужен исходный полный текст из PDF, без предыдущей очистки")

    parent = artifacts.get(artifact["parent_artifact_id"])

    if (
        parent is None
        or parent["representation"] != "pdf"
        or parent["work_id"] != artifact["work_id"]
    ):
        raise ValueError("У исходного текста нет зарегистрированного PDF той же работы")

    source_data = _artifact_bytes(artifact, root)
    _artifact_bytes(parent, root)
    pages = _read_pages(document, artifact, parent, root, validator)

    # Используем именно сборку извлекателя, чтобы не подменить её похожей нормализацией.
    reconstructed = _combine_page_texts(pages, method=artifact["extraction_method"])

    if reconstructed.encode("utf-8") != source_data:
        raise ValueError("Постраничный экспорт не воспроизводит зарегистрированный полный текст")

    excluded: dict[int, str] = {}

    for exclusion in document["exclude_pages"]:
        page_number = exclusion["page_number"]

        if page_number in excluded or page_number > len(pages):
            raise ValueError("Исключаемая страница повторяется или отсутствует в документе")

        excluded[page_number] = exclusion["reason"]

    kept_pages = [
        (page.page_number, page.text)
        for page in pages if page.page_number not in excluded
    ]

    if not kept_pages:
        raise ValueError("Нельзя исключить все страницы документа")

    line_exclusions = tuple(
        LineExclusion(**exclusion) for exclusion in document["exclude_lines"]
    )
    result = clean_pages(
        kept_pages,
        join_words=frozenset(document["join_words"]),
        exclude_lines=line_exclusions,
    )

    if not result.text.strip():
        raise ValueError("После очистки остался пустой текст")

    prefix = f"documents/{artifact['sha256']}"
    output_path = f"{prefix}/text.txt"
    output_data = result.text.encode("utf-8")
    files = {output_path: output_data}

    for page in result.pages:
        files[f"{prefix}/pages/page_{page.page_number:04d}.txt"] = page.text.encode("utf-8")

    report = {
        "work_id": artifact["work_id"],
        "title": works[artifact["work_id"]]["title"],
        "source_artifact_id": artifact["artifact_id"],
        "source_path": artifact["path"],
        "source_sha256": artifact["sha256"],
        "parent_pdf_sha256": parent["sha256"],
        "pages_manifest_path": document["pages_manifest_path"],
        "pages_manifest_sha256": document["pages_manifest_sha256"],
        "output_path": output_path,
        "output_sha256": _sha256(output_data),
        "source_characters": len(source_data.decode("utf-8")),
        "input_page_characters": sum(len(page.text) for page in pages),
        "output_characters": len(result.text),
        "page_count": len(pages),
        "kept_pages": [page.page_number for page in result.pages],
        "excluded_pages": [
            {
                "page_number": page.page_number,
                "reason": excluded[page.page_number],
                "sha256": _sha256(page.text.encode("utf-8")),
                "characters": len(page.text),
            }
            for page in pages if page.page_number in excluded
        ],
        "excluded_lines": [
            asdict(exclusion)
            for exclusion in sorted(
                line_exclusions,
                key=lambda exclusion: (exclusion.page_number, exclusion.line_number),
            )
        ],
        "changes": [asdict(change) for change in result.changes],
        "issues": [asdict(issue) for issue in result.issues],
    }

    return files, report


def _implementation_hashes(schema_dir: Path) -> dict[str, str]:
    """Зафиксировать код преобразований и использованные схемы."""

    paths = {
        name: PROJECT_ROOT / name
        for name in (
            "src/preprocess/text_cleaning.py", "src/preprocess/cleaning_io.py",
            "src/collect/pdf_text.py", "src/collect/base.py",
        )
    }

    for name in ("text_cleaning_plan.schema.json", "pdf_page_export.schema.json"):
        paths[f"manifests/schemas/{name}"] = schema_dir / name

    return {name: sha256_file(path) for name, path in paths.items()}


def prepare_cleaning(
    plan_path: Path,
    *,
    project_root: Path,
    schema_dir: Path | None = None,
) -> PreparedCleaning:
    """Проверить план и подготовить результат в памяти, не создавая файлов."""

    root = project_root.resolve()
    schemas = schema_dir or PROJECT_ROOT / "manifests/schemas"
    plan_data = _checked_path(root, plan_path).read_bytes()
    plan = _read_json(plan_data)
    _validate(plan, _validator(schemas, "text_cleaning_plan.schema.json"), "План очистки")

    if plan["preprocessing_version"] != CLEANING_VERSION:
        raise ValueError(
            f"Версия плана {plan['preprocessing_version']} не поддерживается "
            f"текущей реализацией {CLEANING_VERSION}. Создайте новый план; "
            "исторические планы и пакеты не перезаписывайте."
        )

    page_validator = _validator(schemas, "pdf_page_export.schema.json")

    store = ManifestStore(project_root=root, schema_dir=schemas)
    audit = store.audit()

    if audit.errors or audit.warnings:
        details = "\n".join((*audit.errors, *audit.warnings))
        raise ValueError(f"Очистка остановлена: проверка реестров\n{details}")

    artifacts = {record["artifact_id"]: record for record in store.records("artifacts")}
    works = {record["work_id"]: record for record in store.records("works")}
    seen_works: set[str] = set()
    seen_artifacts: set[str] = set()
    files: dict[str, bytes] = {"plan.json": plan_data}
    documents: list[dict[str, Any]] = []

    for document in plan["documents"]:
        if (
            document["work_id"] in seen_works
            or document["source_artifact_id"] in seen_artifacts
        ):
            raise ValueError("В плане повторяется работа или исходный артефакт")

        seen_works.add(document["work_id"])
        seen_artifacts.add(document["source_artifact_id"])
        document_files, document_report = _prepare_document(
            document, artifacts, works, root, page_validator
        )
        files.update(document_files)
        documents.append(document_report)

    plan_sha256 = _sha256(plan_data)
    report = {
        "schema_version": "text-cleaning-report-v2",
        "preprocessing_version": CLEANING_VERSION,
        "status": "candidate_not_approved",
        "plan_sha256": plan_sha256,
        "implementation_sha256": _implementation_hashes(schemas),
        "documents": documents,
        "totals": {
            "documents": len(documents),
            "pages_before": sum(document["page_count"] for document in documents),
            "pages_after": sum(len(document["kept_pages"]) for document in documents),
            "excluded_pages": sum(len(document["excluded_pages"]) for document in documents),
            "excluded_lines": sum(len(document["excluded_lines"]) for document in documents),
            "changes": sum(len(document["changes"]) for document in documents),
            "issues": sum(len(document["issues"]) for document in documents),
            "changes_by_code": dict(Counter(
                change["code"] for document in documents for change in document["changes"]
            )),
            "issues_by_code": dict(Counter(
                issue["code"] for document in documents for issue in document["issues"]
            )),
        },
    }
    files["report.json"] = _json_bytes(report)
    output_dir = _checked_path(root, OUTPUT_BASE / CLEANING_VERSION / plan_sha256)

    return PreparedCleaning(root, output_dir, files, report)


def _existing_matches(prepared: PreparedCleaning) -> bool:
    """Проверить существующий пакет побайтово, не меняя его время записи."""

    if not prepared.output_dir.exists():
        return False

    if not prepared.output_dir.is_dir():
        raise ValueError(f"Выходной путь уже занят файлом: {prepared.output_dir}")

    actual_files: set[str] = set()

    for path in prepared.output_dir.rglob("*"):
        _checked_path(prepared.project_root, path)

        if path.is_file():
            relative = path.relative_to(prepared.output_dir).as_posix()
            actual_files.add(relative)

            if relative not in prepared.files or path.read_bytes() != prepared.files[relative]:
                raise ValueError(f"Существующий пакет отличается; перезапись запрещена: {path}")

        elif not path.is_dir():
            raise ValueError(f"Необычный объект в выходном каталоге: {path}")

    if actual_files != set(prepared.files):
        raise ValueError("Существующий пакет неполон; перезапись запрещена")

    return True


def publish_cleaning(prepared: PreparedCleaning, *, dry_run: bool = False) -> bool:
    """Записать новый пакет целиком либо подтвердить точный повтор без перезаписи."""

    expected_dir = _checked_path(
        prepared.project_root,
        OUTPUT_BASE / CLEANING_VERSION / _sha256(prepared.files["plan.json"]),
    )

    if prepared.output_dir != expected_dir:
        raise ValueError("Выходной каталог не соответствует версии и SHA-256 плана")

    for relative in prepared.files:
        path = Path(relative)

        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError(f"Недопустимый путь выходного файла: {relative}")

    if _existing_matches(prepared) or dry_run:
        return False

    expected_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = expected_dir.parent / f".{expected_dir.name}.lock"

    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    except FileExistsError as exception:
        raise ValueError("Этот пакет уже записывается другим процессом; повторите позже") from exception

    try:
        if _existing_matches(prepared):
            return False

        with tempfile.TemporaryDirectory(prefix=".cleaning-", dir=expected_dir.parent) as temporary:
            staging = Path(temporary) / "batch"
            staging.mkdir()

            for relative, data in prepared.files.items():
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)

                with path.open("xb") as stream:
                    stream.write(data)

            staging.rename(expected_dir)

    finally:
        os.close(lock_descriptor)
        lock_path.unlink()

    return True
