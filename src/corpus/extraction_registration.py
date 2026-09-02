"""Регистрация текста, извлечённого из локального PDF-артефакта."""

from __future__ import annotations

import copy
import re

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .manifests import (
    ManifestConflictError,
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
    sha256_bytes,
)

EXTRACTION_METHODS = {"pdf", "pdf_ocr_layout"}
SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ARTIFACT_ID_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")


def find_registered_pdf_artifact(
    store: ManifestStore,
    relative_path: str,
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Найти единственный зарегистрированный PDF и проверить его байты."""

    normalized_path, file_path = _resolve_data_file(
        relative_path,
        project_root=project_root,
    )
    matches = [
        artifact
        for artifact in store.records("artifacts")
        if artifact.get("path") == normalized_path
        and artifact.get("representation") == "pdf"
    ]

    if not matches:
        raise ValueError(
            f"Для {normalized_path!r} не найден зарегистрированный PDF-артефакт"
        )

    if len(matches) > 1:
        raise ManifestConflictError(
            f"Для {normalized_path!r} найдено несколько PDF-артефактов"
        )

    artifact = copy.deepcopy(matches[0])
    _validate_parent_artifact(artifact)
    file_bytes = file_path.read_bytes()
    digest = sha256_bytes(file_bytes)

    if artifact["sha256"] != digest:
        raise ManifestConflictError(
            f"SHA-256 файла {normalized_path!r} не совпадает с реестром"
        )

    if artifact["artifact_id"] != f"sha256:{digest}":
        raise ManifestConflictError(
            f"artifact_id файла {normalized_path!r} не совпадает с его SHA-256"
        )

    if artifact["bytes"] != len(file_bytes):
        raise ManifestConflictError(
            f"Размер файла {normalized_path!r} не совпадает с реестром"
        )

    return artifact


def plan_extracted_text(
    parent_artifact: dict[str, Any],
    text: str,
    *,
    extraction_method: str,
    extraction_version: str,
    extracted_at: str,
    existing_artifacts: Iterable[dict[str, Any]] = (),
    ocr_version: str | None = None,
) -> ManifestPlan:
    """Построить план дочернего текстового артефакта без нового получения."""

    _validate_parent_artifact(parent_artifact)

    if not isinstance(text, str) or not text.strip():
        raise ValueError("Извлечённый текст должен быть непустой строкой")

    method = _required_string(
        extraction_method,
        field_name="extraction_method",
    )

    if method not in EXTRACTION_METHODS:
        available = ", ".join(sorted(EXTRACTION_METHODS))
        raise ValueError(
            f"extraction_method={method!r} не поддерживается; доступны: {available}"
        )

    version = normalize_extraction_version(extraction_version)
    timestamp = _normalize_timestamp(extracted_at)
    is_ocr = method == "pdf_ocr_layout"
    normalized_ocr_version = (
        _required_string(ocr_version, field_name="ocr_version")
        if is_ocr
        else None
    )

    if not is_ocr and ocr_version is not None:
        raise ValueError("ocr_version допустима только для метода OCR")

    text_bytes = text.encode("utf-8")
    digest = sha256_bytes(text_bytes)
    artifact_id = f"sha256:{digest}"
    relative_path = (
        Path("data")
        / "extracted"
        / version
        / digest[:2]
        / f"{digest}.txt"
    ).as_posix()
    blob = PlannedBlob(
        relative_path=relative_path,
        data=text_bytes,
        sha256=digest,
    )
    artifact = _build_text_artifact(
        parent_artifact=parent_artifact,
        artifact_id=artifact_id,
        relative_path=relative_path,
        digest=digest,
        byte_count=len(text_bytes),
        character_count=len(text),
        word_count=len(text.split()),
        extraction_method=method,
        extraction_version=version,
        ocr_version=normalized_ocr_version,
        timestamp=timestamp,
    )
    known_artifacts = [
        copy.deepcopy(candidate)
        for candidate in existing_artifacts
    ]
    _validate_unique_extraction_result(artifact, known_artifacts)
    existing = [
        copy.deepcopy(candidate)
        for candidate in known_artifacts
        if candidate.get("artifact_id") == artifact_id
    ]

    if len(existing) > 1:
        raise ManifestConflictError(
            f"В реестре найден повторный artifact_id={artifact_id!r}"
        )

    update_reasons: dict[str, str] = {}

    if existing:
        artifact, updated = _reuse_existing_artifact(existing[0], artifact)

        if updated:
            update_reasons[artifact["artifact_record_id"]] = (
                "Добавлены новые события получения и применимые записи прав "
                "родительского PDF."
            )

    return ManifestPlan(
        artifacts=[artifact],
        artifact_update_reasons=update_reasons,
        blobs=[blob],
    )


def normalize_extraction_version(value: str) -> str:
    """Вернуть безопасную версию метода для поля и компонента пути."""

    version = _required_string(value, field_name="extraction_version")

    if not SAFE_VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "extraction_version может содержать только буквы, цифры, "
            "точку, '_' и '-'"
        )

    return version


def _resolve_data_file(
    value: str,
    *,
    project_root: Path,
) -> tuple[str, Path]:
    """Разрешить существующий относительный путь только внутри ``data/``."""

    raw_path = _required_string(value, field_name="relative_path")
    relative_path = Path(raw_path)

    if relative_path.is_absolute():
        raise ValueError("relative_path должен быть относительным путём")

    root = project_root.resolve()
    data_root = (root / "data").resolve()
    file_path = (root / relative_path).resolve()

    if not file_path.is_relative_to(data_root):
        raise ValueError("relative_path должен указывать файл внутри data/")

    if file_path.suffix.casefold() != ".pdf":
        raise ValueError("relative_path должен указывать PDF-файл")

    if not file_path.is_file():
        raise ValueError(f"PDF-файл не найден: {raw_path}")

    return file_path.relative_to(root).as_posix(), file_path


def _validate_parent_artifact(artifact: dict[str, Any]) -> None:
    """Проверить минимальные инварианты исходного PDF-артефакта."""

    if artifact.get("schema_version") != "artifacts-v1":
        raise ValueError("Родитель должен иметь schema_version='artifacts-v1'")

    if artifact.get("representation") != "pdf":
        raise ValueError("Родительский артефакт должен иметь representation='pdf'")

    if artifact.get("parent_artifact_id") is not None:
        raise ValueError("Родительский PDF должен быть исходным артефактом")

    artifact_id = artifact.get("artifact_id")

    if not isinstance(artifact_id, str) or not ARTIFACT_ID_PATTERN.fullmatch(
        artifact_id
    ):
        raise ValueError("Родительский artifact_id должен содержать SHA-256")

    if artifact.get("sha256") != artifact_id.removeprefix("sha256:"):
        raise ValueError("SHA-256 родительского артефакта не совпадает с artifact_id")

    _required_string(artifact.get("work_id"), field_name="work_id")

    if not isinstance(artifact.get("retrievals"), list):
        raise ValueError("Родительский артефакт должен содержать список retrievals")

    if not isinstance(artifact.get("rights_record_ids"), list):
        raise ValueError(
            "Родительский артефакт должен содержать список rights_record_ids"
        )


def _build_text_artifact(
    *,
    parent_artifact: dict[str, Any],
    artifact_id: str,
    relative_path: str,
    digest: str,
    byte_count: int,
    character_count: int,
    word_count: int,
    extraction_method: str,
    extraction_version: str,
    ocr_version: str | None,
    timestamp: str,
) -> dict[str, Any]:
    """Собрать запись производного текста с происхождением от PDF."""

    is_ocr = extraction_method == "pdf_ocr_layout"

    return {
        "schema_version": "artifacts-v1",
        "created_at": timestamp,
        "artifact_record_id": f"artifact-record:{artifact_id}",
        "artifact_id": artifact_id,
        "work_id": parent_artifact["work_id"],
        "parent_artifact_id": parent_artifact["artifact_id"],
        "retrievals": copy.deepcopy(parent_artifact["retrievals"]),
        "rights_record_ids": list(parent_artifact["rights_record_ids"]),
        "content_role": parent_artifact["content_role"],
        "representation": "ocr_text" if is_ocr else "plain_text",
        "mime_type": "text/plain; charset=utf-8",
        "path": relative_path,
        "sha256": digest,
        "bytes": byte_count,
        "extraction_method": extraction_method,
        "extraction_version": extraction_version,
        "ocr_method": "tesseract" if is_ocr else None,
        "ocr_version": ocr_version,
        "preprocessing_version": None,
        "tokenizer_repo": None,
        "tokenizer_revision": None,
        "characters": character_count,
        "words": word_count,
        "subtokens": None,
        "h2_input_sha256": None,
        "label_leakage_audit_version": None,
        "label_leakage_audit_status": "not_checked",
        "acquisition_method": parent_artifact["acquisition_method"],
        "acquisition_scope": parent_artifact["acquisition_scope"],
        "acquisition_status": parent_artifact["acquisition_status"],
        "extraction_status": "succeeded",
        "qa_status": "not_evaluated",
        "processing_status": "processed",
        "error_code": None,
        "error_detail": None,
        "updated_at": timestamp,
    }


def _reuse_existing_artifact(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Обновить изменяемую проекцию совместимого артефакта."""

    provenance_fields = {
        "work_id",
        "parent_artifact_id",
        "content_role",
        "representation",
        "path",
        "sha256",
        "bytes",
        "extraction_method",
        "extraction_version",
        "ocr_method",
        "ocr_version",
        "acquisition_method",
        "acquisition_scope",
        "acquisition_status",
    }
    conflicts = sorted(
        field_name
        for field_name in provenance_fields
        if existing.get(field_name) != candidate.get(field_name)
    )

    if conflicts:
        fields = ", ".join(conflicts)
        raise ManifestConflictError(
            "Те же байты текста уже зарегистрированы с другим "
            f"происхождением; различаются поля: {fields}"
        )

    merged = copy.deepcopy(existing)
    retrievals = _merge_retrievals(
        existing.get("retrievals", []),
        candidate.get("retrievals", []),
    )
    rights_record_ids = _ordered_union(
        existing.get("rights_record_ids", []),
        candidate.get("rights_record_ids", []),
    )
    updated = False

    if retrievals != existing.get("retrievals", []):
        merged["retrievals"] = retrievals
        updated = True

    if rights_record_ids != existing.get("rights_record_ids", []):
        merged["rights_record_ids"] = rights_record_ids
        updated = True

    if updated:
        merged["updated_at"] = _latest_timestamp(
            existing["updated_at"],
            candidate["updated_at"],
        )

    return merged, updated


def _validate_unique_extraction_result(
    candidate: dict[str, Any],
    existing_artifacts: Iterable[dict[str, Any]],
) -> None:
    """Не допустить два результата одной версии для одного PDF."""

    identity_fields = ("parent_artifact_id", "extraction_version")
    competing = [
        artifact
        for artifact in existing_artifacts
        if artifact.get("artifact_id") != candidate["artifact_id"]
        and artifact.get("representation") in {"plain_text", "ocr_text"}
        and all(
            artifact.get(field_name) == candidate.get(field_name)
            for field_name in identity_fields
        )
    ]

    if competing:
        raise ManifestConflictError(
            "Для одного PDF уже зарегистрирован другой результат той же "
            "версии извлечения; измените extraction_version"
        )


def _merge_retrievals(
    existing: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Объединить проекции получений и выявить конфликт одного ID."""

    result = copy.deepcopy(existing)
    by_id = {record["retrieval_id"]: record for record in result}

    for record in candidate:
        previous = by_id.get(record["retrieval_id"])

        if previous is not None and previous != record:
            raise ManifestConflictError(
                f"retrieval_id={record['retrieval_id']!r} имеет разные проекции"
            )

        if previous is None:
            copied_record = copy.deepcopy(record)
            result.append(copied_record)
            by_id[record["retrieval_id"]] = copied_record

    return sorted(result, key=lambda record: record["retrieval_id"])


def _ordered_union(existing: list[str], candidate: list[str]) -> list[str]:
    """Объединить строки устойчиво, сохранив первое появление."""

    result = list(existing)
    seen = set(result)

    for value in candidate:
        if value in seen:
            continue

        result.append(value)
        seen.add(value)

    return result


def _latest_timestamp(left: str, right: str) -> str:
    """Вернуть более позднюю из двух меток ISO 8601."""

    parsed_left = datetime.fromisoformat(left.replace("Z", "+00:00"))
    parsed_right = datetime.fromisoformat(right.replace("Z", "+00:00"))

    return right if parsed_right > parsed_left else left


def _normalize_timestamp(value: str) -> str:
    """Нормализовать время извлечения с обязательным часовым поясом."""

    raw_timestamp = _required_string(value, field_name="extracted_at")

    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))

    except ValueError as exception:
        raise ValueError("extracted_at должен быть меткой ISO 8601") from exception

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("extracted_at должен содержать часовой пояс")

    return timestamp.isoformat(timespec="microseconds")


def _required_string(value: Any, *, field_name: str) -> str:
    """Вернуть непустую строку или выдать понятную ошибку."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")

    return value.strip()
