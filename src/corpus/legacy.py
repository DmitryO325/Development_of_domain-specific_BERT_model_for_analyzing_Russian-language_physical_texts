"""Преобразование старого Document/JSONL в нормализованные реестры v1."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from src.collect.base import Document

from .identity import canonicalize_url, normalize_doi, resolve_work_identity
from .manifests import ManifestError, ManifestPlan, PlannedBlob, sha256_bytes
from .profiles import SourceProfile

# Получен как uuid5(NAMESPACE_DNS, "ruphysbert.manifests.records.v1") и
# зафиксирован на весь срок жизни схем v1.
RECORD_NAMESPACE = uuid.UUID("5c1efb06-1789-5207-96b9-70e6ccbff651")
ACQUISITION_METHODS = {"manual_download", "api", "crawler", "platform_export", "other"}
ACQUISITION_SCOPES = {"single", "sample", "bulk"}
VERIFIED_PDF_TEXT_SOURCES = {"pdf", "pdf_ocr_layout"}


def normalize_datetime(value: str | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def parse_published_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    try:
        parsed_date = date.fromisoformat(raw)
        return parsed_date.isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def plan_legacy_document(
    document: Document,
    *,
    profile: SourceProfile,
    project_root: Path,
    imported_at: str | datetime,
    acquisition_method: str,
    acquisition_scope: str,
    rights_record_ids: list[str] | None = None,
    include_pdf: bool = True,
) -> ManifestPlan:
    """Сформировать pending-работу и фактически доступные файловые артефакты."""

    if not profile.matches(document.source, document.url):
        raise ManifestError(
            f"Документ source={document.source!r} не соответствует профилю {profile.key!r}"
        )
    if not document.title.strip():
        raise ManifestError("У документа отсутствует название")
    if not document.text.strip():
        raise ManifestError("У документа отсутствует текст")
    if acquisition_method not in ACQUISITION_METHODS:
        raise ManifestError(f"Неизвестный acquisition_method: {acquisition_method!r}")
    if acquisition_scope not in ACQUISITION_SCOPES:
        raise ManifestError(f"Неизвестный acquisition_scope: {acquisition_scope!r}")

    project_root = Path(project_root).resolve()
    timestamp = normalize_datetime(imported_at)
    published_at = parse_published_date(document.published)
    year = published_at[:4] if published_at else document.extra.get("year")
    raw_doi = _first_value(document.extra, "doi", "DOI")
    doi = normalize_doi(raw_doi)
    edn = _first_value(document.extra, "edn", "EDN")
    native_id = profile.native_id(document.url, document.extra)
    identity = resolve_work_identity(
        source_id=profile.source_id,
        title=document.title,
        authors=document.authors,
        year=year,
        doi=doi,
        native_id=native_id,
    )
    canonical_url = canonicalize_url(document.url)

    abstract = _first_value(document.extra, "abstract")
    if not abstract and document.source.casefold().endswith(":rss"):
        abstract = document.text.strip()

    keywords_raw = document.extra.get("keywords", [])
    keywords = (
        [str(value).strip() for value in keywords_raw if str(value).strip()]
        if isinstance(keywords_raw, list)
        else []
    )
    genre = _infer_genre(document.section)
    work = {
        "schema_version": "works-v1",
        "created_at": timestamp,
        "work_id": identity.work_id,
        "work_aliases": [],
        "source_group_id": profile.source_group_id,
        "source_id": profile.source_id,
        "platform": profile.platform,
        "journal_id": profile.journal_id,
        "journal_title": profile.journal_title,
        "canonical_url": canonical_url,
        "doi": doi,
        "edn": edn,
        "identity_confidence": identity.confidence,
        "title": document.title.strip(),
        "authors": [author.strip() for author in document.authors if author.strip()],
        "abstract": abstract,
        "keywords": keywords,
        "published_at": published_at,
        "language": document.language or "und",
        "genre": genre,
        "pacs_codes_raw": [],
        "udc_codes_raw": [],
        "publisher_section": document.section,
        "duplicate_of_work_id": None,
        "eligibility_status": "pending",
        "exclusion_reason": None,
        "updated_at": timestamp,
    }

    plan = ManifestPlan(works=[work])
    rights_ids = list(dict.fromkeys(rights_record_ids or []))
    if not rights_ids:
        plan.warnings.append(
            f"{identity.work_id}: права не привязаны; артефакты нельзя повышать до eligible"
        )
    if identity.confidence == "low":
        plan.warnings.append(
            f"{identity.work_id}: резервный UUIDv5; требуется проверить DOI или ID источника"
        )
    if year and (not str(year).isdigit() or not 2000 <= int(year) <= 2025):
        plan.warnings.append(
            f"{identity.work_id}: год {year!r} вне утверждённого диапазона 2000–2025"
        )
    plan.warnings.append(
        f"{identity.work_id}: старый сборщик не сохранил HTTP-заголовки и точное время "
        "получения; массив retrievals оставлен пустым"
    )

    parent_artifact_id: str | None = None
    if include_pdf and document.extra.get("pdf_path"):
        pdf_plan = _plan_pdf(
            document,
            work_id=identity.work_id,
            project_root=project_root,
            timestamp=timestamp,
            rights_record_ids=rights_ids,
            acquisition_method=acquisition_method,
            acquisition_scope=acquisition_scope,
        )
        if pdf_plan is None:
            plan.warnings.append(
                f"{identity.work_id}: указанный PDF не найден; зарегистрирован только текст"
            )
        else:
            artifact, blob = pdf_plan
            plan.artifacts.append(artifact)
            plan.blobs.append(blob)
            parent_artifact_id = artifact["artifact_id"]

    text_source = str(document.extra.get("text_source") or "legacy_text").casefold()
    text_artifact, text_blob = _plan_text(
        document,
        work_id=identity.work_id,
        timestamp=timestamp,
        rights_record_ids=rights_ids,
        # HTML fallback и RSS получены не из локального PDF.
        parent_artifact_id=(
            parent_artifact_id if text_source in VERIFIED_PDF_TEXT_SOURCES else None
        ),
        acquisition_method=acquisition_method,
        acquisition_scope=acquisition_scope,
    )
    plan.artifacts.append(text_artifact)
    plan.blobs.append(text_blob)
    return plan


def _plan_pdf(
    document: Document,
    *,
    work_id: str,
    project_root: Path,
    timestamp: str,
    rights_record_ids: list[str],
    acquisition_method: str,
    acquisition_scope: str,
) -> tuple[dict[str, Any], PlannedBlob] | None:
    source_path = Path(str(document.extra["pdf_path"]))
    if not source_path.is_absolute():
        source_path = project_root / source_path
    source_path = source_path.resolve()
    data_root = (project_root / "data").resolve()
    if not source_path.is_relative_to(data_root):
        raise ManifestError(f"Исходный PDF находится за пределами data/: {source_path}")
    if not source_path.is_file():
        return None

    data = source_path.read_bytes()
    digest = sha256_bytes(data)
    relative_path = f"data/raw/sha256/{digest[:2]}/{digest}.pdf"
    artifact_id = f"sha256:{digest}"
    artifact = _artifact_record(
        work_id=work_id,
        artifact_id=artifact_id,
        parent_artifact_id=None,
        # Старый сборщик не сохранил точные время, HTTP-статус и заголовки.
        retrievals=[],
        rights_record_ids=rights_record_ids,
        acquisition_method=acquisition_method,
        acquisition_scope=acquisition_scope,
        content_role="full_text",
        representation="pdf",
        mime_type="application/pdf",
        relative_path=relative_path,
        data=data,
        timestamp=timestamp,
        extraction_method=None,
        extraction_version=None,
        ocr_method=None,
        characters=None,
        words=None,
        extraction_status="not_started",
        processing_status="not_started",
    )
    return artifact, PlannedBlob(relative_path, data, digest)


def _plan_text(
    document: Document,
    *,
    work_id: str,
    timestamp: str,
    rights_record_ids: list[str],
    parent_artifact_id: str | None,
    acquisition_method: str,
    acquisition_scope: str,
) -> tuple[dict[str, Any], PlannedBlob]:
    data = document.text.encode("utf-8")
    digest = sha256_bytes(data)
    relative_path = f"data/extracted/legacy-v1/{digest[:2]}/{digest}.txt"
    artifact_id = f"sha256:{digest}"
    is_rss = document.source.casefold().endswith(":rss")
    text_source = str(document.extra.get("text_source") or "legacy_text").casefold()
    verified_pdf_text = (
        text_source in VERIFIED_PDF_TEXT_SOURCES and parent_artifact_id is not None
    )
    is_ocr = text_source == "pdf_ocr_layout"
    representation = "ocr_text" if is_ocr else "plain_text"
    extraction_method = "legacy_rss_html_to_text" if is_rss else f"legacy_{text_source}"
    is_metadata = is_rss or not verified_pdf_text
    artifact = _artifact_record(
        work_id=work_id,
        artifact_id=artifact_id,
        parent_artifact_id=parent_artifact_id,
        retrievals=[],
        rights_record_ids=rights_record_ids,
        acquisition_method=acquisition_method,
        acquisition_scope=acquisition_scope,
        content_role="metadata_only" if is_metadata else "full_text",
        representation=representation,
        mime_type="text/plain; charset=utf-8",
        relative_path=relative_path,
        data=data,
        timestamp=timestamp,
        extraction_method=extraction_method,
        extraction_version="legacy-import-v1",
        ocr_method=text_source if is_ocr else None,
        characters=len(document.text),
        words=len(re.findall(r"\S+", document.text)),
        extraction_status="succeeded",
        processing_status="processed",
    )
    return artifact, PlannedBlob(relative_path, data, digest)


def _artifact_record(
    *,
    work_id: str,
    artifact_id: str,
    parent_artifact_id: str | None,
    retrievals: list[dict[str, Any]],
    rights_record_ids: list[str],
    acquisition_method: str,
    acquisition_scope: str,
    content_role: str,
    representation: str,
    mime_type: str,
    relative_path: str,
    data: bytes,
    timestamp: str,
    extraction_method: str | None,
    extraction_version: str | None,
    ocr_method: str | None,
    characters: int | None,
    words: int | None,
    extraction_status: str,
    processing_status: str,
) -> dict[str, Any]:
    digest = sha256_bytes(data)
    if artifact_id != f"sha256:{digest}":
        raise ManifestError("artifact_id не соответствует фактическим байтам")
    record_key = "\x1f".join((work_id, content_role, representation, artifact_id))
    record_id = f"artifact-record:{uuid.uuid5(RECORD_NAMESPACE, record_key)}"
    return {
        "schema_version": "artifacts-v1",
        "created_at": timestamp,
        "artifact_record_id": record_id,
        "artifact_id": artifact_id,
        "work_id": work_id,
        "parent_artifact_id": parent_artifact_id,
        "retrievals": retrievals,
        "rights_record_ids": rights_record_ids,
        "content_role": content_role,
        "representation": representation,
        "mime_type": mime_type,
        "path": relative_path,
        "sha256": digest,
        "bytes": len(data),
        "extraction_method": extraction_method,
        "extraction_version": extraction_version,
        "ocr_method": ocr_method,
        "ocr_version": None,
        "preprocessing_version": None,
        "tokenizer_repo": None,
        "tokenizer_revision": None,
        "characters": characters,
        "words": words,
        "subtokens": None,
        "h2_input_sha256": None,
        "label_leakage_audit_version": None,
        "label_leakage_audit_status": "not_checked",
        "acquisition_method": acquisition_method,
        "acquisition_scope": acquisition_scope,
        "acquisition_status": "retrieved",
        "extraction_status": extraction_status,
        "qa_status": "not_evaluated",
        "processing_status": processing_status,
        "error_code": None,
        "error_detail": None,
        "updated_at": timestamp,
    }


def _first_value(extra: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = extra.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _infer_genre(section: str | None) -> str:
    normalized = (section or "").casefold()
    if "обзор" in normalized:
        return "review_article"
    if "кратк" in normalized or "short communication" in normalized:
        return "short_communication"
    return "other"
