"""Регистрация вручную загруженных локальных PDF-файлов."""

from __future__ import annotations

import copy

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .identity import (
    canonicalize_url,
    normalize_doi,
    normalize_native_id,
    resolve_work_identity,
)
from .manifests import ManifestPlan, canonical_json, sha256_bytes
from .profiles import SourceProfile
from .registration import RegistrationOptions

GENRES = {"research_article", "review_article", "short_communication", "other"}
ELIGIBILITY_STATUSES = {"pending", "eligible", "rejected", "quarantined"}


@dataclass(frozen=True)
class LocalFileRegistration:
    """Явные метаданные одного вручную загруженного PDF-файла."""

    relative_path: str
    source_url: str
    canonical_url: str
    retrieved_at: str
    title: str
    authors: list[str]
    doi: str | None
    published_at: str | None
    section: str | None
    language: str
    genre: str
    abstract: str | None
    keywords: list[str]
    pacs_codes_raw: list[dict[str, Any]]
    udc_codes_raw: list[dict[str, Any]]
    acquisition_agent: str
    eligibility_status: str
    exclusion_reason: str | None


def plan_local_file(
    registration: LocalFileRegistration,
    profile: SourceProfile,
    options: RegistrationOptions,
    *,
    project_root: Path,
) -> ManifestPlan:
    """Построить план регистрации существующего PDF без копирования байтов."""

    _validate_options(options)

    root = Path(project_root).resolve()
    relative_path, file_path = _resolve_pdf_path(
        registration.relative_path,
        project_root=root,
    )
    source_url = _canonical_http_url(
        registration.source_url,
        field_name="source_url",
    )
    canonical_url = _canonical_http_url(
        registration.canonical_url,
        field_name="canonical_url",
    )
    retrieved_at = _normalize_timestamp(registration.retrieved_at)
    published_at = _normalize_published_at(registration.published_at)
    title = _required_string(registration.title, field_name="title")
    authors = _normalize_string_list(registration.authors, field_name="authors")
    language = _required_string(registration.language, field_name="language")
    genre = _validate_choice("genre", registration.genre, GENRES)
    abstract = _optional_string(registration.abstract)
    section = _optional_string(registration.section)
    keywords = _normalize_string_list(registration.keywords, field_name="keywords")
    acquisition_agent = _required_string(
        registration.acquisition_agent,
        field_name="acquisition_agent",
    )
    eligibility_status, exclusion_reason = _normalize_eligibility(
        registration.eligibility_status,
        registration.exclusion_reason,
        abstract=abstract,
        language=language,
        genre=genre,
        published_at=published_at,
    )
    doi = _normalize_doi(registration.doi)

    if not profile.matches("", canonical_url):
        raise ValueError(
            f"canonical_url={canonical_url!r} не соответствует "
            f"профилю {profile.key!r}"
        )

    native_id = normalize_native_id(profile.native_id(canonical_url, {}))

    if native_id is None:
        raise ValueError(
            f"Профиль {profile.key!r} не определил source_native_id "
            f"из canonical_url={canonical_url!r}"
        )

    file_bytes = file_path.read_bytes()

    if not file_bytes.startswith(b"%PDF-"):
        raise ValueError(f"Файл {relative_path!r} не имеет PDF-сигнатуры")

    digest = sha256_bytes(file_bytes)
    artifact_id = f"sha256:{digest}"
    artifact_record_id = f"artifact-record:sha256:{digest}"
    identity = resolve_work_identity(
        source_id=profile.source_id,
        title=title,
        authors=authors,
        year=published_at[:4] if published_at else None,
        doi=doi,
        native_id=native_id,
    )
    alias_value = f"{profile.source_id}:{native_id}"
    alias_identity = f"source_native_id:{alias_value}"
    provenance_sha256 = _manual_provenance_sha256(
        source_url=source_url,
        retrieved_at=retrieved_at,
        acquisition_agent=acquisition_agent,
    )
    retrieval_event = _build_retrieval_event(
        work_id=identity.work_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        acquisition_agent=acquisition_agent,
        relative_path=relative_path,
        digest=digest,
        byte_count=len(file_bytes),
        provenance_sha256=provenance_sha256,
        rights_record_ids=list(options.rights_record_ids),
    )
    retrieval = _retrieval_projection(retrieval_event)
    work = _build_work(
        registration=registration,
        profile=profile,
        work_id=identity.work_id,
        identity_confidence=identity.confidence,
        canonical_url=canonical_url,
        doi=doi,
        published_at=published_at,
        title=title,
        authors=authors,
        abstract=abstract,
        keywords=keywords,
        section=section,
        language=language,
        genre=genre,
        eligibility_status=eligibility_status,
        exclusion_reason=exclusion_reason,
        alias_identity=alias_identity,
        timestamp=retrieved_at,
    )
    artifact = _build_artifact(
        artifact_record_id=artifact_record_id,
        artifact_id=artifact_id,
        work_id=identity.work_id,
        retrieval=retrieval,
        rights_record_ids=list(options.rights_record_ids),
        relative_path=relative_path,
        digest=digest,
        byte_count=len(file_bytes),
        timestamp=retrieved_at,
    )
    alias = _build_alias_record(
        work_id=identity.work_id,
        alias_value=alias_value,
        timestamp=retrieved_at,
        evidence_sha256=provenance_sha256,
        retrieval_id=retrieval_event["retrieval_id"],
    )

    return ManifestPlan(
        works=[work],
        artifacts=[artifact],
        retrieval_events=[retrieval_event],
        work_aliases=[alias],
    )


def _validate_options(options: RegistrationOptions) -> None:
    """Отклонить параметры, которые искажают ручное получение PDF."""

    expected = {
        "content_role": "full_text",
        "acquisition_method": "manual_download",
        "acquisition_scope": "sample",
        "response_representation": "pdf",
        "request_context_type": "work",
    }

    for field_name, expected_value in expected.items():
        actual_value = getattr(options, field_name)

        if actual_value != expected_value:
            raise ValueError(
                f"options.{field_name} должен быть "
                f"{expected_value!r}, получено {actual_value!r}"
            )


def _resolve_pdf_path(
    value: str,
    *,
    project_root: Path,
) -> tuple[str, Path]:
    """Разрешить существующий PDF только внутри каталога ``data/``."""

    raw_path = _required_string(value, field_name="relative_path")
    relative_path = Path(raw_path)

    if relative_path.is_absolute():
        raise ValueError("relative_path должен быть относительным путём")

    data_root = (project_root / "data").resolve()
    file_path = (project_root / relative_path).resolve()

    if not file_path.is_relative_to(data_root):
        raise ValueError("relative_path должен указывать файл внутри data/")

    if file_path.suffix.casefold() != ".pdf":
        raise ValueError("relative_path должен указывать PDF-файл")

    if not file_path.is_file():
        raise ValueError(f"PDF-файл не найден: {raw_path}")

    normalized_path = file_path.relative_to(project_root).as_posix()

    return normalized_path, file_path


def _canonical_http_url(value: str, *, field_name: str) -> str:
    """Нормализовать полный HTTP(S)-URL без параметров слежения."""

    raw_url = _required_string(value, field_name=field_name)

    try:
        canonical_url = canonicalize_url(raw_url)
        parts = urlsplit(canonical_url)

    except ValueError as exception:
        raise ValueError(f"{field_name} содержит некорректный URL") from exception

    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"{field_name} должен быть полным HTTP(S)-URL")

    return canonical_url


def _normalize_timestamp(value: str) -> str:
    """Нормализовать время ручного получения с часовым поясом."""

    raw_timestamp = _required_string(value, field_name="retrieved_at")

    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))

    except ValueError as exception:
        raise ValueError("retrieved_at должен быть меткой ISO 8601") from exception

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("retrieved_at должен содержать часовой пояс")

    return timestamp.isoformat(timespec="microseconds")


def _normalize_published_at(value: str | None) -> str | None:
    """Проверить необязательную дату публикации в формате ISO."""

    normalized = _optional_string(value)

    if normalized is None:
        return

    try:
        published_at = date.fromisoformat(normalized)

    except ValueError as exception:
        raise ValueError("published_at должен быть датой ISO 8601") from exception

    if published_at.isoformat() != normalized:
        raise ValueError("published_at должен иметь формат YYYY-MM-DD")

    return normalized


def _normalize_doi(value: str | None) -> str | None:
    """Нормализовать DOI и отклонить непустое невалидное значение."""

    raw_doi = _optional_string(value)

    if raw_doi is None:
        return

    doi = normalize_doi(raw_doi)

    if doi is None:
        raise ValueError(f"doi={raw_doi!r} не является корректным DOI")

    return doi


def _required_string(value: Any, *, field_name: str) -> str:
    """Вернуть непустую строку или выдать понятную ошибку."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")

    return value.strip()


def _optional_string(value: Any) -> str | None:
    """Вернуть непустую строку или ``None``."""

    if value is None:
        return

    if not isinstance(value, str):
        raise ValueError("Необязательное текстовое поле должно быть строкой")

    normalized = value.strip()

    return normalized or None


def _normalize_string_list(values: Any, *, field_name: str) -> list[str]:
    """Проверить и очистить список уникальных строк."""

    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} должен быть списком строк")

    result: list[str] = []

    for value in values:
        normalized = _required_string(value, field_name=f"{field_name}[]")

        if normalized not in result:
            result.append(normalized)

    return result


def _validate_choice(name: str, value: str, choices: set[str]) -> str:
    """Проверить строковое значение по конечному набору."""

    normalized = _required_string(value, field_name=name)

    if normalized not in choices:
        available = ", ".join(sorted(choices))
        raise ValueError(f"{name}={normalized!r} не поддерживается; доступны: {available}")

    return normalized


def _normalize_eligibility(
    status: str,
    exclusion_reason: str | None,
    *,
    abstract: str | None,
    language: str,
    genre: str,
    published_at: str | None,
) -> tuple[str, str | None]:
    """Проверить статус допуска и связанную причину исключения."""

    normalized_status = _validate_choice(
        "eligibility_status",
        status,
        ELIGIBILITY_STATUSES,
    )
    normalized_reason = _optional_string(exclusion_reason)

    if normalized_status in {"rejected", "quarantined"}:
        if normalized_reason is None:
            raise ValueError(
                "exclusion_reason обязателен для rejected и quarantined"
            )

        return normalized_status, normalized_reason

    if normalized_reason is not None:
        raise ValueError(
            "exclusion_reason должен быть пустым для pending и eligible"
        )

    if normalized_status == "eligible":
        if abstract is None:
            raise ValueError("eligible-работа должна иметь abstract")

        if language != "ru":
            raise ValueError("eligible-работа должна иметь language='ru'")

        if genre == "other":
            raise ValueError("Для eligible-работы нужен научный genre")

        if published_at is None or not 2000 <= int(published_at[:4]) <= 2025:
            raise ValueError(
                "eligible-работа должна иметь published_at за 2000–2025 год"
            )

    return normalized_status, None


def _manual_provenance_sha256(
    *,
    source_url: str,
    retrieved_at: str,
    acquisition_agent: str,
) -> str:
    """Вычислить хеш явных метаданных ручной загрузки."""

    metadata = {
        "acquisition_agent": acquisition_agent,
        "acquisition_method": "manual_download",
        "retrieved_at": retrieved_at,
        "source_url": source_url,
    }

    return sha256_bytes(canonical_json(metadata).encode("utf-8"))


def _build_retrieval_event(
    *,
    work_id: str,
    source_url: str,
    retrieved_at: str,
    acquisition_agent: str,
    relative_path: str,
    digest: str,
    byte_count: int,
    provenance_sha256: str,
    rights_record_ids: list[str],
) -> dict[str, Any]:
    """Создать событие успешной ручной загрузки без выдуманных HTTP-полей."""

    event_core = {
        "request_context_type": "work",
        "request_context_id": work_id,
        "source_group_id": None,
        "requested_url": source_url,
        "final_url": None,
        "retrieved_at": retrieved_at,
        "acquisition_method": "manual_download",
        "acquisition_scope": "sample",
        "rights_record_ids": rights_record_ids,
        "http_status": None,
        "response_headers": {},
        "response_metadata_sha256": provenance_sha256,
        "response_sha256": digest,
        "response_bytes": byte_count,
        "outcome": "succeeded",
    }
    retrieval_id = _deterministic_id("retrieval", event_core)

    return {
        "schema_version": "retrieval-events-v1",
        "retrieval_id": retrieval_id,
        "created_at": retrieved_at,
        **event_core,
        "acquisition_agent": acquisition_agent,
        "response_path": relative_path,
        "error_code": None,
        "error_detail": None,
    }


def _retrieval_projection(event: dict[str, Any]) -> dict[str, Any]:
    """Построить ссылку PDF-артефакта на событие получения."""

    return {
        "retrieval_id": event["retrieval_id"],
        "retrieved_url": event["requested_url"],
        "retrieved_at": event["retrieved_at"],
        "response_metadata_sha256": event["response_metadata_sha256"],
    }


def _build_work(
    *,
    registration: LocalFileRegistration,
    profile: SourceProfile,
    work_id: str,
    identity_confidence: str,
    canonical_url: str,
    doi: str | None,
    published_at: str | None,
    title: str,
    authors: list[str],
    abstract: str | None,
    keywords: list[str],
    section: str | None,
    language: str,
    genre: str,
    eligibility_status: str,
    exclusion_reason: str | None,
    alias_identity: str,
    timestamp: str,
) -> dict[str, Any]:
    """Собрать карточку работы из явных локальных метаданных."""

    return {
        "schema_version": "works-v1",
        "created_at": timestamp,
        "work_id": work_id,
        "work_aliases": [alias_identity],
        "source_group_id": profile.source_group_id,
        "source_id": profile.source_id,
        "platform": profile.platform,
        "journal_id": profile.journal_id,
        "journal_title": profile.journal_title,
        "canonical_url": canonical_url,
        "doi": doi,
        "edn": None,
        "identity_confidence": identity_confidence,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "keywords": keywords,
        "published_at": published_at,
        "language": language,
        "genre": genre,
        "pacs_codes_raw": copy.deepcopy(registration.pacs_codes_raw),
        "udc_codes_raw": copy.deepcopy(registration.udc_codes_raw),
        "publisher_section": section,
        "duplicate_of_work_id": None,
        "eligibility_status": eligibility_status,
        "exclusion_reason": exclusion_reason,
        "updated_at": timestamp,
    }


def _build_artifact(
    *,
    artifact_record_id: str,
    artifact_id: str,
    work_id: str,
    retrieval: dict[str, Any],
    rights_record_ids: list[str],
    relative_path: str,
    digest: str,
    byte_count: int,
    timestamp: str,
) -> dict[str, Any]:
    """Собрать карточку неизменного исходного PDF-артефакта."""

    return {
        "schema_version": "artifacts-v1",
        "created_at": timestamp,
        "artifact_record_id": artifact_record_id,
        "artifact_id": artifact_id,
        "work_id": work_id,
        "parent_artifact_id": None,
        "retrievals": [retrieval],
        "rights_record_ids": rights_record_ids,
        "content_role": "full_text",
        "representation": "pdf",
        "mime_type": "application/pdf",
        "path": relative_path,
        "sha256": digest,
        "bytes": byte_count,
        "extraction_method": None,
        "extraction_version": None,
        "ocr_method": None,
        "ocr_version": None,
        "preprocessing_version": None,
        "tokenizer_repo": None,
        "tokenizer_revision": None,
        "characters": None,
        "words": None,
        "subtokens": None,
        "h2_input_sha256": None,
        "label_leakage_audit_version": None,
        "label_leakage_audit_status": "not_checked",
        "acquisition_method": "manual_download",
        "acquisition_scope": "sample",
        "acquisition_status": "retrieved",
        "extraction_status": "not_started",
        "qa_status": "not_evaluated",
        "processing_status": "not_started",
        "error_code": None,
        "error_detail": None,
        "updated_at": timestamp,
    }


def _build_alias_record(
    *,
    work_id: str,
    alias_value: str,
    timestamp: str,
    evidence_sha256: str,
    retrieval_id: str,
) -> dict[str, Any]:
    """Создать проверенный псевдоним source_native_id."""

    payload = {
        "work_id": work_id,
        "alias_type": "source_native_id",
        "alias_value": alias_value,
        "verified_at": timestamp,
        "evidence_sha256": evidence_sha256,
        "source_retrieval_id": retrieval_id,
    }

    return {
        "schema_version": "work-aliases-v1",
        "alias_record_id": _deterministic_id("work-alias", payload),
        "created_at": timestamp,
        **payload,
        "supersedes_alias_record_id": None,
    }


def _deterministic_id(prefix: str, payload: dict[str, Any]) -> str:
    """Построить идентификатор из канонического JSON-содержимого."""

    digest = sha256_bytes(canonical_json(payload).encode("utf-8"))

    return f"{prefix}:{digest}"
