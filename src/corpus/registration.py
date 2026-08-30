"""Прямое формирование и согласование реестровых планов корпуса."""

from __future__ import annotations

import copy
import re

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.collect.base import Document, HttpResponseSnapshot

from .identity import (
    canonicalize_url,
    normalize_doi,
    normalize_identity_text,
    normalize_native_id,
    resolve_work_identity,
)
from .manifests import (
    PROJECT_SUBJECT_ID,
    ManifestConflictError,
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
    canonical_json,
    sha256_bytes,
)
from .profiles import SourceProfile

CONTENT_ROLES = {
    "full_text",
    "title_abstract",
    "metadata_only",
    "formula_context",
}
ACQUISITION_METHODS = {
    "manual_download",
    "api",
    "crawler",
    "platform_export",
    "other",
}
ACQUISITION_SCOPES = {"single", "sample", "bulk"}
REPRESENTATIONS = {"pdf", "html", "xml", "json", "rss", "plain_text", "ocr_text"}
REQUEST_CONTEXT_TYPES = {"source", "work", "artifact"}
GENRES = {"research_article", "review_article", "short_communication", "other"}
IDENTITY_CONFLICT_FIELDS = {
    "doi",
    "edn",
    "title",
    "published_at",
    "journal_id",
    "journal_title",
}
LIST_WORK_FIELDS = {
    "work_aliases",
    "authors",
    "keywords",
    "pacs_codes_raw",
    "udc_codes_raw",
}
REPRESENTATION_EXTENSIONS = {
    "pdf": "pdf",
    "html": "html",
    "xml": "xml",
    "json": "json",
    "rss": "xml",
    "plain_text": "txt",
    "ocr_text": "txt",
}
REPRESENTATION_MIME_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html",
    "xml": "application/xml",
    "json": "application/json",
    "rss": "application/rss+xml",
    "plain_text": "text/plain; charset=utf-8",
    "ocr_text": "text/plain; charset=utf-8",
}
IDENTITY_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
SOURCE_SCOPE_SPECIFICITY = {
    "source_group": 1,
    "journal": 2,
    "source": 3,
}
RIGHTS_SCOPE_SPECIFICITY = {
    **SOURCE_SCOPE_SPECIFICITY,
    "work": 4,
    "artifact": 5,
}


@dataclass(frozen=True)
class RegistrationOptions:
    """Явные параметры происхождения и роли регистрируемого документа."""

    content_role: str
    acquisition_method: str
    acquisition_scope: str
    rights_record_ids: tuple[str, ...]
    extraction_method: str
    extraction_version: str
    response_representation: str
    request_context_type: str

    def __post_init__(self) -> None:
        """Нормализовать параметры и отклонить неподдерживаемые значения."""

        _validate_choice("content_role", self.content_role, CONTENT_ROLES)
        _validate_choice(
            "acquisition_method",
            self.acquisition_method,
            ACQUISITION_METHODS,
        )
        _validate_choice(
            "acquisition_scope",
            self.acquisition_scope,
            ACQUISITION_SCOPES,
        )
        _validate_choice(
            "response_representation",
            self.response_representation,
            REPRESENTATIONS,
        )
        _validate_choice(
            "request_context_type",
            self.request_context_type,
            REQUEST_CONTEXT_TYPES,
        )

        rights_record_ids = tuple(
            dict.fromkeys(str(record_id).strip() for record_id in self.rights_record_ids)
        )

        if not rights_record_ids or any(not record_id for record_id in rights_record_ids):
            raise ValueError("rights_record_ids должен содержать непустые ID прав")

        extraction_method = self.extraction_method.strip()
        extraction_version = self.extraction_version.strip()

        if not extraction_method:
            raise ValueError("extraction_method не может быть пустым")

        if not extraction_version:
            raise ValueError("extraction_version не может быть пустым")

        object.__setattr__(self, "rights_record_ids", rights_record_ids)
        object.__setattr__(self, "extraction_method", extraction_method)
        object.__setattr__(self, "extraction_version", extraction_version)


def plan_document(
    document: Document,
    profile: SourceProfile,
    options: RegistrationOptions,
    collected_at: str,
    response: HttpResponseSnapshot | None = None,
) -> ManifestPlan:
    """Построить детерминированный план регистрации одного документа."""

    timestamp = _normalize_timestamp(collected_at, field_name="collected_at")
    canonical_url = _canonical_http_url(document.url, field_name="document.url")
    _validate_document(document, profile, canonical_url)

    _validate_response_provenance(
        document,
        options,
        response,
        canonical_url=canonical_url,
    )

    published_at = _normalize_published(document.published)
    identity_year = published_at[:4] if published_at else document.extra.get("year")
    native_id = profile.native_id(canonical_url, document.extra)
    doi = normalize_doi(_optional_string(document.extra.get("doi")))
    edn = _normalize_edn(document.extra.get("edn"))
    identity = resolve_work_identity(
        source_id=profile.source_id,
        title=document.title,
        authors=_string_list(document.authors),
        year=identity_year,
        doi=doi,
        native_id=native_id,
    )

    text_bytes = document.text.encode("utf-8")
    text_sha256 = sha256_bytes(text_bytes)
    text_artifact_id = f"sha256:{text_sha256}"
    text_artifact_record_id = _artifact_record_id(text_sha256)
    response_sha256 = sha256_bytes(response.body) if response is not None else None
    response_artifact_record_id = (
        _artifact_record_id(response_sha256) if response_sha256 is not None else None
    )

    request_context_id = _request_context_id(
        options.request_context_type,
        profile=profile,
        work_id=identity.work_id,
        response_artifact_record_id=response_artifact_record_id,
        text_artifact_record_id=text_artifact_record_id,
    )

    retrieval_event, response_blob = _build_retrieval_event(
        document=document,
        options=options,
        collected_at=timestamp,
        request_context_id=request_context_id,
        source_group_id=(
            profile.source_group_id
            if options.request_context_type == "source"
            else None
        ),
        response=response,
    )
    retrieval = _retrieval_projection(retrieval_event)
    work = _build_work(
        document=document,
        profile=profile,
        work_id=identity.work_id,
        identity_confidence=identity.confidence,
        canonical_url=canonical_url,
        doi=doi,
        edn=edn,
        published_at=published_at,
        options=options,
        timestamp=timestamp,
    )

    normalized_native_id = normalize_native_id(native_id)

    if normalized_native_id:
        native_alias_value = f"{profile.source_id}:{normalized_native_id}"
        work["work_aliases"].append(
            f"source_native_id:{native_alias_value}"
        )

    artifacts: list[dict[str, Any]] = []
    blobs: list[PlannedBlob] = []
    parent_artifact_id: str | None = None

    if response is not None:
        if response_blob is None:
            raise RuntimeError(
                "Для HTTP-снимка не был сформирован неизменяемый объект ответа"
            )

        blobs.append(response_blob)

        if options.request_context_type != "source":
            response_artifact = _build_response_artifact(
                work_id=identity.work_id,
                options=options,
                response=response,
                response_blob=response_blob,
                retrieval=retrieval,
                timestamp=timestamp,
            )
            artifacts.append(response_artifact)
            parent_artifact_id = response_artifact["artifact_id"]

    text_blob = _build_text_blob(
        data=text_bytes,
        digest=text_sha256,
        extraction_version=options.extraction_version,
    )
    text_artifact = _build_text_artifact(
        document=document,
        work_id=identity.work_id,
        options=options,
        text_blob=text_blob,
        retrieval=retrieval,
        parent_artifact_id=parent_artifact_id,
        timestamp=timestamp,
    )

    if not any(
        artifact["artifact_id"] == text_artifact["artifact_id"]
        for artifact in artifacts
    ):
        artifacts.append(text_artifact)
        blobs.append(text_blob)

    else:
        artifacts[0] = _merge_same_payload_artifact(
            artifacts[0],
            text_artifact,
        )

    work_aliases: list[dict[str, Any]] = []

    if normalized_native_id:
        work_aliases.append(
            _build_alias_record(
                work_id=identity.work_id,
                alias_type="source_native_id",
                alias_value=native_alias_value,
                timestamp=timestamp,
                evidence_sha256=retrieval_event["response_metadata_sha256"],
                retrieval_id=retrieval_event["retrieval_id"],
            )
        )

    return ManifestPlan(
        works=[work],
        artifacts=artifacts,
        retrieval_events=[retrieval_event],
        work_aliases=work_aliases,
        blobs=_unique_blobs(blobs),
    )


def reconcile_document_plan(
    store: ManifestStore,
    plan: ManifestPlan,
) -> tuple[ManifestPlan, dict[str, str]]:
    """Согласовать план с текущими снимками и вернуть CAS-хеши снимков."""

    expected_snapshot_hashes = store.snapshot_hashes()
    reconciled = copy.deepcopy(plan)
    existing_works = store.records("works")
    existing_artifacts = store.records("artifacts")
    existing_aliases = store.records("work_aliases")

    work_id_mapping: dict[str, str] = {}

    for index, candidate in enumerate(reconciled.works):
        existing = _find_existing_work(
            candidate,
            existing_works=existing_works,
            existing_aliases=existing_aliases,
            candidate_aliases=reconciled.work_aliases,
        )

        if existing is None:
            continue

        old_work_id = candidate["work_id"]
        work_id_mapping[old_work_id] = existing["work_id"]
        merged, conflicts, changed = _merge_work(existing, candidate)
        retrieval_ids = _plan_retrieval_ids(reconciled)

        alias_specs = _new_identity_alias_specs(existing, candidate)
        alias_identities = [
            _alias_identity_value(alias_type, alias_value)
            for alias_type, alias_value in alias_specs
        ]

        # Поздний устойчивый идентификатор остаётся проверенным, даже если
        # другая часть карточки требует ручного разрешения конфликта.
        if not conflicts and old_work_id != existing["work_id"]:
            alias_identities.insert(0, old_work_id)

        aliases = _ordered_union(
            merged.get("work_aliases", []),
            alias_identities,
        )

        if aliases != merged.get("work_aliases", []):
            merged["work_aliases"] = aliases
            changed = True

        _add_verified_identity_aliases(
            reconciled,
            alias_specs=alias_specs,
            timestamp=candidate["updated_at"],
            preserved_work_id=existing["work_id"],
        )

        if conflicts:
            merged["eligibility_status"] = "quarantined"
            merged["exclusion_reason"] = (
                "Конфликт идентичности: " + ", ".join(sorted(conflicts)) + "."
            )
            changed = True
            reconciled.identity_conflicts.extend(
                _build_identity_conflict_records(
                    work_id=existing["work_id"],
                    conflicts=conflicts,
                    retrieval_ids=retrieval_ids,
                    timestamp=candidate["updated_at"],
                )
            )

        if changed:
            merged["created_at"] = existing["created_at"]
            merged["updated_at"] = _next_timestamp(
                existing["updated_at"],
                candidate["updated_at"],
            )
            reconciled.work_update_reasons[existing["work_id"]] = (
                _work_update_reason(conflicts)
            )

        else:
            merged = copy.deepcopy(existing)

        reconciled.works[index] = merged

    _remap_plan_work_ids(reconciled, work_id_mapping)
    _drop_unmaterialized_alias_records(reconciled)
    _reconcile_artifacts(reconciled, existing_artifacts)
    reconciled.work_aliases = _deduplicate_history(
        reconciled.work_aliases,
        primary_field="alias_record_id",
    )
    reconciled.identity_conflicts = _deduplicate_history(
        reconciled.identity_conflicts,
        primary_field="conflict_id",
    )

    return reconciled, expected_snapshot_hashes


def resolve_collection_rights(
    store: ManifestStore,
    profile: SourceProfile,
    *,
    acquisition_method: str,
    acquisition_scope: str,
    allowed_rights_record_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Проверить права до сетевого сбора и вернуть определяющие записи."""

    _validate_choice(
        "acquisition_method",
        acquisition_method,
        ACQUISITION_METHODS,
    )
    _validate_choice(
        "acquisition_scope",
        acquisition_scope,
        ACQUISITION_SCOPES,
    )

    allowed_ids = {
        str(record_id).strip()
        for record_id in allowed_rights_record_ids
        if str(record_id).strip()
    }

    if not allowed_ids:
        raise ValueError(
            "Для прямой регистрации укажите хотя бы один rights_record_id"
        )

    store.preflight([], allow_unresolved_rights=True)
    rights = {
        record["rights_record_id"]: record
        for record in store.records("rights")
    }
    missing_ids = allowed_ids - set(rights)

    if missing_ids:
        missing = ", ".join(sorted(missing_ids))
        raise ManifestConflictError(
            f"В реестре rights отсутствуют явно указанные записи: {missing}"
        )

    superseded_ids = {
        record["supersedes_rights_record_id"]
        for record in rights.values()
        if record.get("supersedes_rights_record_id")
    }
    applicable = [
        record
        for record_id, record in rights.items()
        if record_id not in superseded_ids
        and _right_applies_to_source(record, profile)
    ]

    _reject_known_specific_blocks(
        store,
        profile,
        rights=list(rights.values()),
        superseded_ids=superseded_ids,
        acquisition_method=acquisition_method,
        acquisition_scope=acquisition_scope,
    )

    selected: list[dict[str, Any]] = []

    for operation in ("acquisition", "storage"):
        controlling = _controlling_source_rights(
            applicable,
            operation=operation,
            acquisition_method=acquisition_method,
            acquisition_scope=acquisition_scope,
        )

        if not controlling:
            raise ManifestConflictError(
                f"Для источника {profile.source_id!r} нет применимой записи "
                f"права на операцию {operation}"
            )

        controlling_ids = {
            record["rights_record_id"] for record in controlling
        }
        unapproved_ids = controlling_ids - allowed_ids

        if unapproved_ids:
            unapproved = ", ".join(sorted(unapproved_ids))
            raise ManifestConflictError(
                "Наиболее конкретные записи прав не были явно разрешены "
                f"для этого запуска: {unapproved}"
            )

        blocked = [
            record
            for record in controlling
            if not _source_right_permits(store, profile, record)
        ]

        if blocked:
            details = ", ".join(
                f"{record['rights_record_id']}={record['status']}"
                for record in sorted(
                    blocked,
                    key=lambda item: item["rights_record_id"],
                )
            )
            raise ManifestConflictError(
                f"Операция {operation} не разрешена: {details}"
            )

        selected.extend(controlling)

    return tuple(
        sorted({record["rights_record_id"] for record in selected})
    )


def _right_applies_to_source(
    right: dict[str, Any],
    profile: SourceProfile,
) -> bool:
    """Проверить применимость области права до получения отдельной работы."""

    expected_scope_ids = {
        "source_group": profile.source_group_id,
        "source": profile.source_id,
        "journal": profile.journal_id,
    }
    scope_type = right["scope_type"]

    return (
        scope_type in expected_scope_ids
        and right["scope_id"] == expected_scope_ids[scope_type]
    )


def _reject_known_specific_blocks(
    store: ManifestStore,
    profile: SourceProfile,
    *,
    rights: list[dict[str, Any]],
    superseded_ids: set[str],
    acquisition_method: str,
    acquisition_scope: str,
) -> None:
    """Отклонить обход, если известный объект имеет более узкий запрет."""

    active_rights = [
        right
        for right in rights
        if right["rights_record_id"] not in superseded_ids
    ]
    works = [
        work
        for work in store.records("works")
        if work["source_group_id"] == profile.source_group_id
        and work["source_id"] == profile.source_id
        and work["journal_id"] == profile.journal_id
    ]
    artifacts_by_work: dict[str, list[dict[str, Any]]] = {}

    for artifact in store.records("artifacts"):
        artifacts_by_work.setdefault(artifact["work_id"], []).append(artifact)

    for work in works:
        contexts: list[dict[str, Any] | None] = [None]
        contexts.extend(artifacts_by_work.get(work["work_id"], []))

        for artifact in contexts:
            for operation in ("acquisition", "storage"):
                controlling = _controlling_known_rights(
                    active_rights,
                    work=work,
                    artifact=artifact,
                    operation=operation,
                    acquisition_method=acquisition_method,
                    acquisition_scope=acquisition_scope,
                )

                if not controlling or all(
                    RIGHTS_SCOPE_SPECIFICITY[right["scope_type"]]
                    <= SOURCE_SCOPE_SPECIFICITY["source"]
                    for right in controlling
                ):
                    continue

                blocked = [
                    right
                    for right in controlling
                    if not _known_right_permits(store, profile, work, artifact, right)
                ]

                if not blocked:
                    continue

                target = (
                    artifact["artifact_record_id"]
                    if artifact is not None
                    else work["work_id"]
                )
                details = ", ".join(
                    f"{right['rights_record_id']}={right['status']}"
                    for right in sorted(
                        blocked,
                        key=lambda item: item["rights_record_id"],
                    )
                )
                raise ManifestConflictError(
                    f"До сетевого обхода обнаружен более конкретный "
                    f"запрет {operation} для {target!r}: {details}"
                )


def _controlling_known_rights(
    rights: list[dict[str, Any]],
    *,
    work: dict[str, Any],
    artifact: dict[str, Any] | None,
    operation: str,
    acquisition_method: str,
    acquisition_scope: str,
) -> list[dict[str, Any]]:
    """Выбрать определяющие права для известной работы или артефакта."""

    candidates = [
        right
        for right in rights
        if right["operation"] == operation
        and _right_applies_to_known_target(right, work, artifact)
        and (
            operation != "acquisition"
            or (
                right["acquisition_method"] == acquisition_method
                and right["acquisition_scope"] == acquisition_scope
            )
        )
    ]

    if not candidates:
        return []

    specificity = max(
        RIGHTS_SCOPE_SPECIFICITY[right["scope_type"]]
        for right in candidates
    )

    return [
        right
        for right in candidates
        if RIGHTS_SCOPE_SPECIFICITY[right["scope_type"]] == specificity
    ]


def _right_applies_to_known_target(
    right: dict[str, Any],
    work: dict[str, Any],
    artifact: dict[str, Any] | None,
) -> bool:
    """Сопоставить область права с известным объектом источника."""

    scope_type = right["scope_type"]
    scope_id = right["scope_id"]
    expected = {
        "source_group": work["source_group_id"],
        "source": work["source_id"],
        "journal": work["journal_id"],
        "work": work["work_id"],
    }

    if scope_type == "artifact":
        return artifact is not None and scope_id in {
            artifact["artifact_record_id"],
            artifact.get("artifact_id"),
        }

    if scope_type == "work":
        return scope_id in {work["work_id"], *work.get("work_aliases", [])}

    return expected[scope_type] == scope_id


def _known_right_permits(
    store: ManifestStore,
    profile: SourceProfile,
    work: dict[str, Any],
    artifact: dict[str, Any] | None,
    right: dict[str, Any],
) -> bool:
    """Проверить узкое право и его условия до сетевого обхода."""

    expires_at = right.get("rights_expires_at")

    if expires_at and date.fromisoformat(expires_at) < date.today():
        return False

    if right["status"] == "allowed":
        return True

    if right["status"] != "conditional":
        return False

    if (
        right.get("conditions_satisfied_at")
        and right.get("conditions_evidence_sha256")
    ):
        return True

    active = _active_condition_fulfilments(store)

    return all(
        any(
            fulfilment["rights_record_id"] == right["rights_record_id"]
            and fulfilment["condition"] == condition
            and _fulfilment_applies_to_known_target(
                fulfilment,
                profile,
                work,
                artifact,
            )
            for fulfilment in active
        )
        for condition in right["rights_conditions"]
    )


def _active_condition_fulfilments(store: ManifestStore) -> list[dict[str, Any]]:
    """Вернуть действующие незаменённые выполнения условий."""

    fulfilments = store.records("condition_fulfilments")
    superseded_ids = {
        record["supersedes_fulfilment_id"]
        for record in fulfilments
        if record.get("supersedes_fulfilment_id")
    }
    now = datetime.now(timezone.utc)
    active: list[dict[str, Any]] = []

    for fulfilment in fulfilments:
        if (
            fulfilment["fulfilment_id"] in superseded_ids
            or fulfilment["status"] != "satisfied"
            or datetime.fromisoformat(
                fulfilment["satisfied_at"].replace("Z", "+00:00")
            ) > now
        ):
            continue

        expires = fulfilment.get("expires_at")

        if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < now:
            continue

        active.append(fulfilment)

    return active


def _fulfilment_applies_to_known_target(
    fulfilment: dict[str, Any],
    profile: SourceProfile,
    work: dict[str, Any],
    artifact: dict[str, Any] | None,
) -> bool:
    """Сопоставить выполнение условия с узким контекстом."""

    subject_type = fulfilment["subject_type"]
    subject_id = fulfilment["subject_id"]

    if subject_type == "project":
        return subject_id == PROJECT_SUBJECT_ID

    if subject_type == "source":
        return subject_id in {profile.source_group_id, profile.source_id}

    if subject_type == "work":
        return subject_id in {work["work_id"], *work.get("work_aliases", [])}

    if subject_type == "artifact":
        return artifact is not None and subject_id in {
            artifact["artifact_record_id"],
            artifact.get("artifact_id"),
        }

    return False


def _controlling_source_rights(
    rights: list[dict[str, Any]],
    *,
    operation: str,
    acquisition_method: str,
    acquisition_scope: str,
) -> list[dict[str, Any]]:
    """Выбрать наиболее конкретные права для одной операции источника."""

    candidates = [
        right
        for right in rights
        if right["operation"] == operation
        and (
            (
                operation != "acquisition"
                and right["acquisition_method"] is None
                and right["acquisition_scope"] is None
            )
            or (
                right["acquisition_method"] == acquisition_method
                and right["acquisition_scope"] == acquisition_scope
            )
        )
    ]

    if not candidates:
        return []

    specificity = max(
        (
            SOURCE_SCOPE_SPECIFICITY[right["scope_type"]],
            int(right["acquisition_method"] is not None),
        )
        for right in candidates
    )

    return [
        right
        for right in candidates
        if (
            SOURCE_SCOPE_SPECIFICITY[right["scope_type"]],
            int(right["acquisition_method"] is not None),
        )
        == specificity
    ]


def _source_right_permits(
    store: ManifestStore,
    profile: SourceProfile,
    right: dict[str, Any],
) -> bool:
    """Проверить статус, срок и точные условия одного права источника."""

    expires_at = right.get("rights_expires_at")

    if expires_at and date.fromisoformat(expires_at) < date.today():
        return False

    if right["status"] == "allowed":
        return True

    if right["status"] != "conditional":
        return False

    if (
        right.get("conditions_satisfied_at")
        and right.get("conditions_evidence_sha256")
    ):
        return True

    fulfilments = store.records("condition_fulfilments")
    superseded_ids = {
        record["supersedes_fulfilment_id"]
        for record in fulfilments
        if record.get("supersedes_fulfilment_id")
    }
    now = datetime.now(timezone.utc)
    active = []

    for fulfilment in fulfilments:
        if (
            fulfilment["fulfilment_id"] in superseded_ids
            or fulfilment["status"] != "satisfied"
            or fulfilment["rights_record_id"] != right["rights_record_id"]
        ):
            continue

        expires = fulfilment.get("expires_at")
        satisfied_at = datetime.fromisoformat(
            fulfilment["satisfied_at"].replace("Z", "+00:00")
        )

        if satisfied_at > now:
            continue

        if expires:
            expires_at_value = datetime.fromisoformat(
                expires.replace("Z", "+00:00")
            )

            if expires_at_value < now:
                continue

        if _fulfilment_applies_to_source(fulfilment, profile):
            active.append(fulfilment)

    return all(
        any(fulfilment["condition"] == condition for fulfilment in active)
        for condition in right["rights_conditions"]
    )


def _fulfilment_applies_to_source(
    fulfilment: dict[str, Any],
    profile: SourceProfile,
) -> bool:
    """Сопоставить выполнение условия с проектом или источником запуска."""

    if fulfilment["subject_type"] == "project":
        return fulfilment["subject_id"] == PROJECT_SUBJECT_ID

    return (
        fulfilment["subject_type"] == "source"
        and fulfilment["subject_id"]
        in {profile.source_id, profile.source_group_id}
    )


def _validate_choice(name: str, value: str, choices: set[str]) -> None:
    """Проверить одно строковое значение по конечному набору вариантов."""

    if value not in choices:
        available = ", ".join(sorted(choices))
        raise ValueError(f"{name}={value!r} не поддерживается; доступны: {available}")


def _validate_document(
    document: Document,
    profile: SourceProfile,
    canonical_url: str,
) -> None:
    """Проверить минимальные поля документа и соответствие профилю."""

    if not document.title.strip():
        raise ValueError("document.title не может быть пустым")

    if not document.text:
        raise ValueError("document.text не может быть пустым")

    if not profile.matches(document.source, canonical_url):
        raise ValueError(
            f"Документ source={document.source!r} не соответствует "
            f"профилю {profile.key!r}"
        )


def _validate_response_provenance(
    document: Document,
    options: RegistrationOptions,
    response: HttpResponseSnapshot | None,
    *,
    canonical_url: str,
) -> None:
    """Связать HTTP-снимок с документом и проверить полный текст."""

    if options.content_role == "full_text" and (
        response is None or options.request_context_type == "source"
    ):
        raise ValueError(
            "Роль full_text требует сохранённый HttpResponseSnapshot "
            "исходного представления одной работы"
        )

    if response is None:
        return

    requested_url = _canonical_http_url(
        response.requested_url,
        field_name="response.requested_url",
    )

    if options.request_context_type != "source":
        allowed_urls = {canonical_url}

        if document.pdf_url:
            allowed_urls.add(
                _canonical_http_url(
                    document.pdf_url,
                    field_name="document.pdf_url",
                )
            )

        if requested_url not in allowed_urls:
            raise ValueError(
                "response.requested_url не соответствует document.url "
                "или document.pdf_url"
            )

    if options.content_role == "full_text" and not response.body:
        raise ValueError("Роль full_text требует непустое тело HTTP-ответа")


def _canonical_http_url(value: str, *, field_name: str) -> str:
    """Нормализовать HTTP(S)-адрес и отклонить неполный URL."""

    canonical = canonicalize_url(value)
    parts = urlsplit(canonical)

    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"{field_name} должен быть полным HTTP(S)-адресом")

    return canonical


def _normalize_timestamp(value: str, *, field_name: str) -> str:
    """Нормализовать временную метку с обязательным часовым поясом."""

    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))

    except (AttributeError, TypeError, ValueError) as exception:
        raise ValueError(f"{field_name} должен быть временной меткой ISO 8601") from exception

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать часовой пояс")

    return timestamp.isoformat(timespec="microseconds")


def _normalize_published(value: str | None) -> str | None:
    """Преобразовать известные представления даты публикации в ISO date."""

    if not value or not value.strip():
        return

    raw = value.strip()

    try:
        return date.fromisoformat(raw).isoformat()

    except ValueError:
        pass

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()

    except ValueError:
        pass

    try:
        return parsedate_to_datetime(raw).date().isoformat()

    except (TypeError, ValueError, OverflowError):
        pass

    for pattern in ("%d.%m.%Y", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(raw, pattern)

        except ValueError:
            continue

        return parsed.date().isoformat()

    return


def _optional_string(value: Any) -> str | None:
    """Вернуть непустое строковое представление либо ``None``."""

    if value is None:
        return

    normalized = str(value).strip()

    return normalized or None


def _normalize_edn(value: Any) -> str | None:
    """Нормализовать EDN без изменения содержащихся в нём символов."""

    normalized = _optional_string(value)

    return normalized.upper() if normalized else None


def _string_list(values: Any) -> list[str]:
    """Собрать уникальный список непустых строк без угадывания структуры."""

    if not isinstance(values, (list, tuple)):
        return []

    result: list[str] = []

    for value in values:
        normalized = _optional_string(value)

        if normalized and normalized not in result:
            result.append(normalized)

    return result


def _build_work(
    *,
    document: Document,
    profile: SourceProfile,
    work_id: str,
    identity_confidence: str,
    canonical_url: str,
    doi: str | None,
    edn: str | None,
    published_at: str | None,
    options: RegistrationOptions,
    timestamp: str,
) -> dict[str, Any]:
    """Сформировать одну запись ``works-v1`` из явных полей документа."""

    explicit_abstract = _optional_string(document.extra.get("abstract"))
    abstract = (
        document.text.strip()
        if options.content_role == "title_abstract"
        else explicit_abstract
    )
    genre = _optional_string(document.extra.get("genre")) or "other"

    if genre not in GENRES:
        genre = "other"

    return {
        "schema_version": "works-v1",
        "created_at": timestamp,
        "work_id": work_id,
        "work_aliases": [],
        "source_group_id": profile.source_group_id,
        "source_id": profile.source_id,
        "platform": profile.platform,
        "journal_id": profile.journal_id,
        "journal_title": profile.journal_title,
        "canonical_url": canonical_url,
        "doi": doi,
        "edn": edn,
        "identity_confidence": identity_confidence,
        "title": document.title.strip(),
        "authors": _string_list(document.authors),
        "abstract": abstract,
        "keywords": _string_list(document.extra.get("keywords")),
        "published_at": published_at,
        "language": document.language.strip() or "und",
        "genre": genre,
        "pacs_codes_raw": [],
        "udc_codes_raw": [],
        "publisher_section": _optional_string(document.section),
        "duplicate_of_work_id": None,
        "eligibility_status": "pending",
        "exclusion_reason": None,
        "updated_at": timestamp,
    }


def _request_context_id(
    context_type: str,
    *,
    profile: SourceProfile,
    work_id: str,
    response_artifact_record_id: str | None,
    text_artifact_record_id: str,
) -> str:
    """Выбрать идентификатор сущности, в контексте которой сделан запрос."""

    if context_type == "source":
        return profile.source_id

    if context_type == "work":
        return work_id

    return response_artifact_record_id or text_artifact_record_id


def _build_retrieval_event(
    *,
    document: Document,
    options: RegistrationOptions,
    collected_at: str,
    request_context_id: str,
    source_group_id: str | None,
    response: HttpResponseSnapshot | None,
) -> tuple[dict[str, Any], PlannedBlob | None]:
    """Создать честное событие получения и необязательный HTTP-объект."""

    rights_record_ids = list(options.rights_record_ids)

    if response is None:
        metadata = {
            "document_url": _canonical_http_url(
                document.url,
                field_name="document.url",
            ),
            "http_response_available": False,
            "registered_at": collected_at,
        }
        metadata_sha256 = sha256_bytes(canonical_json(metadata).encode("utf-8"))
        event_core = {
            "request_context_type": options.request_context_type,
            "source_group_id": source_group_id,
            "requested_url": document.url.strip(),
            "retrieved_at": collected_at,
            "response_metadata_sha256": metadata_sha256,
        }
        retrieval_id = _deterministic_id("retrieval", event_core)
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": retrieval_id,
            "created_at": collected_at,
            "request_context_type": options.request_context_type,
            "request_context_id": request_context_id,
            "source_group_id": source_group_id,
            "requested_url": document.url.strip(),
            "final_url": None,
            "retrieved_at": collected_at,
            "acquisition_method": options.acquisition_method,
            "acquisition_scope": options.acquisition_scope,
            "rights_record_ids": rights_record_ids,
            "http_status": None,
            "response_headers": {},
            "response_metadata_sha256": metadata_sha256,
            "response_path": None,
            "response_sha256": None,
            "response_bytes": None,
            "outcome": "metadata_only",
            "error_code": None,
            "error_detail": None,
        }

        return event, None

    requested_url = _canonical_http_url(
        response.requested_url,
        field_name="response.requested_url",
    )
    final_url = _canonical_http_url(
        response.final_url,
        field_name="response.final_url",
    )
    retrieved_at = _normalize_timestamp(
        response.retrieved_at,
        field_name="response.retrieved_at",
    )

    if not 200 <= response.status_code <= 399:
        raise ValueError("Успешный HttpResponseSnapshot должен иметь HTTP 2xx или 3xx")

    response_sha256 = sha256_bytes(response.body)
    response_path = _content_path(
        root="data/raw/http",
        digest=response_sha256,
        extension=REPRESENTATION_EXTENSIONS[options.response_representation],
    )
    response_blob = PlannedBlob(
        relative_path=response_path,
        data=response.body,
        sha256=response_sha256,
    )
    response_headers = _response_headers(response.headers)
    metadata_sha256 = response.metadata_sha256()
    event_core = {
        "request_context_type": options.request_context_type,
        "request_context_id": request_context_id,
        "source_group_id": source_group_id,
        "requested_url": requested_url,
        "final_url": final_url,
        "retrieved_at": retrieved_at,
        "acquisition_method": options.acquisition_method,
        "acquisition_scope": options.acquisition_scope,
        "rights_record_ids": rights_record_ids,
        "http_status": response.status_code,
        "response_headers": response_headers,
        "response_metadata_sha256": metadata_sha256,
        "response_sha256": response_sha256,
        "response_bytes": len(response.body),
        "outcome": "succeeded",
    }
    retrieval_id = _deterministic_id("retrieval", event_core)
    event = {
        "schema_version": "retrieval-events-v1",
        "retrieval_id": retrieval_id,
        "created_at": retrieved_at,
        "request_context_type": options.request_context_type,
        "request_context_id": request_context_id,
        "source_group_id": source_group_id,
        "requested_url": requested_url,
        "final_url": final_url,
        "retrieved_at": retrieved_at,
        "acquisition_method": options.acquisition_method,
        "acquisition_scope": options.acquisition_scope,
        "rights_record_ids": rights_record_ids,
        "http_status": response.status_code,
        "response_headers": response_headers,
        "response_metadata_sha256": metadata_sha256,
        "response_path": response_path,
        "response_sha256": response_sha256,
        "response_bytes": len(response.body),
        "outcome": "succeeded",
        "error_code": None,
        "error_detail": None,
    }

    return event, response_blob


def _response_headers(headers: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Материализовать безопасные заголовки, сохранив повторные значения."""

    grouped: dict[str, list[str]] = {}

    for raw_name, raw_value in headers:
        name = raw_name.strip().casefold()

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            continue

        grouped.setdefault(name, []).append(raw_value.strip())

    return {
        name: ", ".join(values)
        for name, values in sorted(grouped.items())
    }


def _retrieval_projection(event: dict[str, Any]) -> dict[str, Any]:
    """Построить материализованную ссылку артефакта на событие получения."""

    return {
        "retrieval_id": event["retrieval_id"],
        "retrieved_url": event.get("final_url") or event["requested_url"],
        "retrieved_at": event["retrieved_at"],
        "response_metadata_sha256": event["response_metadata_sha256"],
    }


def _retrieval_event_id(event: dict[str, Any]) -> str:
    """Пересчитать ID события после канонической замены его контекста."""

    if event["outcome"] == "metadata_only":
        identity_payload = {
            "request_context_type": event["request_context_type"],
            "source_group_id": event["source_group_id"],
            "requested_url": event["requested_url"],
            "retrieved_at": event["retrieved_at"],
            "response_metadata_sha256": event["response_metadata_sha256"],
        }

    else:
        identity_payload = {
            "request_context_type": event["request_context_type"],
            "request_context_id": event["request_context_id"],
            "source_group_id": event["source_group_id"],
            "requested_url": event["requested_url"],
            "final_url": event["final_url"],
            "retrieved_at": event["retrieved_at"],
            "acquisition_method": event["acquisition_method"],
            "acquisition_scope": event["acquisition_scope"],
            "rights_record_ids": event["rights_record_ids"],
            "http_status": event["http_status"],
            "response_headers": event["response_headers"],
            "response_metadata_sha256": event["response_metadata_sha256"],
            "response_sha256": event["response_sha256"],
            "response_bytes": event["response_bytes"],
            "outcome": event["outcome"],
        }

    return _deterministic_id("retrieval", identity_payload)


def _build_response_artifact(
    *,
    work_id: str,
    options: RegistrationOptions,
    response: HttpResponseSnapshot,
    response_blob: PlannedBlob,
    retrieval: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    """Описать неизменяемое тело HTTP-ответа как исходный артефакт работы."""

    digest = response_blob.sha256
    mime_type = _response_mime_type(response, options.response_representation)

    return _artifact_record(
        artifact_record_id=_artifact_record_id(digest),
        artifact_id=f"sha256:{digest}",
        work_id=work_id,
        parent_artifact_id=None,
        retrievals=[retrieval],
        rights_record_ids=list(options.rights_record_ids),
        content_role=options.content_role,
        representation=options.response_representation,
        mime_type=mime_type,
        path=response_blob.relative_path,
        digest=digest,
        byte_count=len(response_blob.data),
        extraction_method=None,
        extraction_version=None,
        characters=None,
        words=None,
        extraction_status="not_started",
        processing_status="not_started",
        options=options,
        timestamp=timestamp,
    )


def _response_mime_type(
    response: HttpResponseSnapshot,
    representation: str,
) -> str:
    """Выбрать фактический Content-Type либо безопасный тип представления."""

    for name, value in response.headers:
        if name.casefold() == "content-type" and value.strip():
            return value.strip()

    return REPRESENTATION_MIME_TYPES[representation]


def _build_text_blob(
    *,
    data: bytes,
    digest: str,
    extraction_version: str,
) -> PlannedBlob:
    """Подготовить производный текст по безопасному адресу на основе хеша."""

    version = _safe_path_component(extraction_version)
    path = _content_path(
        root=f"data/extracted/{version}",
        digest=digest,
        extension="txt",
    )

    return PlannedBlob(relative_path=path, data=data, sha256=digest)


def _build_text_artifact(
    *,
    document: Document,
    work_id: str,
    options: RegistrationOptions,
    text_blob: PlannedBlob,
    retrieval: dict[str, Any],
    parent_artifact_id: str | None,
    timestamp: str,
) -> dict[str, Any]:
    """Описать извлечённый текст и его связь с исходным ответом."""

    text_source = str(document.extra.get("text_source", "")).casefold()
    method = options.extraction_method.casefold()
    representation = "ocr_text" if "ocr" in text_source or "ocr" in method else "plain_text"

    return _artifact_record(
        artifact_record_id=_artifact_record_id(text_blob.sha256),
        artifact_id=f"sha256:{text_blob.sha256}",
        work_id=work_id,
        parent_artifact_id=parent_artifact_id,
        retrievals=[retrieval],
        rights_record_ids=list(options.rights_record_ids),
        content_role=options.content_role,
        representation=representation,
        mime_type="text/plain; charset=utf-8",
        path=text_blob.relative_path,
        digest=text_blob.sha256,
        byte_count=len(text_blob.data),
        extraction_method=options.extraction_method,
        extraction_version=options.extraction_version,
        characters=len(document.text),
        words=len(document.text.split()),
        extraction_status="succeeded",
        processing_status="processed",
        options=options,
        timestamp=timestamp,
    )


def _artifact_record(
    *,
    artifact_record_id: str,
    artifact_id: str,
    work_id: str,
    parent_artifact_id: str | None,
    retrievals: list[dict[str, Any]],
    rights_record_ids: list[str],
    content_role: str,
    representation: str,
    mime_type: str,
    path: str,
    digest: str,
    byte_count: int,
    extraction_method: str | None,
    extraction_version: str | None,
    characters: int | None,
    words: int | None,
    extraction_status: str,
    processing_status: str,
    options: RegistrationOptions,
    timestamp: str,
) -> dict[str, Any]:
    """Собрать полную запись ``artifacts-v1`` без неявных допущений."""

    return {
        "schema_version": "artifacts-v1",
        "created_at": timestamp,
        "artifact_record_id": artifact_record_id,
        "artifact_id": artifact_id,
        "work_id": work_id,
        "parent_artifact_id": parent_artifact_id,
        "retrievals": retrievals,
        "rights_record_ids": rights_record_ids,
        "content_role": content_role,
        "representation": representation,
        "mime_type": mime_type,
        "path": path,
        "sha256": digest,
        "bytes": byte_count,
        "extraction_method": extraction_method,
        "extraction_version": extraction_version,
        "ocr_method": None,
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
        "acquisition_method": options.acquisition_method,
        "acquisition_scope": options.acquisition_scope,
        "acquisition_status": "retrieved",
        "extraction_status": extraction_status,
        "qa_status": "not_evaluated",
        "processing_status": processing_status,
        "error_code": None,
        "error_detail": None,
        "updated_at": timestamp,
    }


def _merge_same_payload_artifact(
    response_artifact: dict[str, Any],
    text_artifact: dict[str, Any],
) -> dict[str, Any]:
    """Не создавать две записи, когда ответ уже равен производному тексту."""

    merged = copy.deepcopy(text_artifact)
    merged["path"] = response_artifact["path"]
    merged["parent_artifact_id"] = None
    merged["mime_type"] = response_artifact["mime_type"]

    return merged


def _artifact_record_id(digest: str) -> str:
    """Построить устойчивый идентификатор строки артефакта по SHA-256."""

    return f"artifact-record:sha256:{digest}"


def _content_path(*, root: str, digest: str, extension: str) -> str:
    """Построить безопасный относительный путь содержимо-адресуемого объекта."""

    return str(Path(root) / digest[:2] / f"{digest}.{extension}")


def _safe_path_component(value: str) -> str:
    """Преобразовать версию метода в один безопасный компонент пути."""

    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")

    if not normalized:
        raise ValueError("extraction_version не образует безопасный компонент пути")

    return normalized


def _deterministic_id(prefix: str, payload: dict[str, Any]) -> str:
    """Получить детерминированный ID из канонического JSON-содержимого."""

    digest = sha256_bytes(canonical_json(payload).encode("utf-8"))

    return f"{prefix}:{digest}"


def _build_alias_record(
    *,
    work_id: str,
    alias_type: str,
    alias_value: str,
    timestamp: str,
    evidence_sha256: str,
    retrieval_id: str | None,
) -> dict[str, Any]:
    """Создать неизменяемую запись о проверенном псевдониме работы."""

    payload = {
        "work_id": work_id,
        "alias_type": alias_type,
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


def _unique_blobs(blobs: list[PlannedBlob]) -> list[PlannedBlob]:
    """Оставить по одному экземпляру каждого неизменяемого файла."""

    result: dict[str, PlannedBlob] = {}

    for blob in blobs:
        previous = result.get(blob.relative_path)

        if previous is not None and previous.data != blob.data:
            raise ManifestConflictError(
                f"Путь {blob.relative_path!r} назначен разным байтам"
            )

        result[blob.relative_path] = blob

    return list(result.values())


def _find_existing_work(
    candidate: dict[str, Any],
    *,
    existing_works: list[dict[str, Any]],
    existing_aliases: list[dict[str, Any]],
    candidate_aliases: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Найти одну существующую работу по ID и устойчивым псевдонимам."""

    matches: dict[str, dict[str, Any]] = {}
    candidate_identifiers = {
        candidate["work_id"],
        *candidate.get("work_aliases", []),
        f"url:{canonicalize_url(candidate['canonical_url'])}",
    }

    candidate_alias_values = {
        (record["alias_type"], str(record["alias_value"]).casefold())
        for record in candidate_aliases
        if record.get("work_id") == candidate["work_id"]
    }

    alias_owners: dict[tuple[str, str], str] = {}

    for record in existing_aliases:
        key = (record["alias_type"], str(record["alias_value"]).casefold())
        alias_owners[key] = record["work_id"]

    for existing in existing_works:
        existing_identifiers = {
            existing["work_id"],
            *existing.get("work_aliases", []),
        }
        direct_match = bool(candidate_identifiers & existing_identifiers)
        doi_match = bool(
            candidate.get("doi")
            and normalize_doi(candidate.get("doi"))
            == normalize_doi(existing.get("doi"))
        )
        edn_match = bool(
            candidate.get("edn")
            and str(candidate["edn"]).casefold()
            == str(existing.get("edn") or "").casefold()
        )
        url_match = (
            canonicalize_url(candidate["canonical_url"])
            == canonicalize_url(existing["canonical_url"])
        )
        history_match = any(
            alias_owners.get(alias) == existing["work_id"]
            for alias in candidate_alias_values
        )

        if direct_match or doi_match or edn_match or url_match or history_match:
            matches[existing["work_id"]] = existing

    if len(matches) > 1:
        identifiers = ", ".join(sorted(matches))
        raise ManifestConflictError(
            "Кандидат соответствует нескольким работам: " + identifiers
        )

    return next(iter(matches.values()), None)


def _merge_work(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, tuple[Any, Any]], bool]:
    """Объединить только совместимые поля одной карточки работы."""

    merged = copy.deepcopy(existing)
    conflicts: dict[str, tuple[Any, Any]] = {}
    changed = False

    for field in LIST_WORK_FIELDS:
        values = _ordered_union(existing.get(field, []), candidate.get(field, []))

        if values != existing.get(field, []):
            merged[field] = values
            changed = True

    for field, candidate_value in candidate.items():
        if field in LIST_WORK_FIELDS or field in {
            "schema_version",
            "created_at",
            "updated_at",
            "work_id",
            "eligibility_status",
            "exclusion_reason",
        }:
            continue

        existing_value = existing.get(field)

        if _is_empty(existing_value) and not _is_empty(candidate_value):
            merged[field] = copy.deepcopy(candidate_value)
            changed = True
            continue

        if _is_empty(candidate_value) or _compatible_value(
            field,
            existing_value,
            candidate_value,
        ):
            continue

        if field in IDENTITY_CONFLICT_FIELDS:
            conflicts[field] = (existing_value, candidate_value)

    existing_confidence = existing.get("identity_confidence", "low")
    candidate_confidence = candidate.get("identity_confidence", "low")

    if (
        IDENTITY_CONFIDENCE_RANK[candidate_confidence]
        > IDENTITY_CONFIDENCE_RANK[existing_confidence]
    ):
        merged["identity_confidence"] = candidate_confidence
        changed = True

    return merged, conflicts, changed


def _ordered_union(left: list[Any], right: list[Any]) -> list[Any]:
    """Объединить списки устойчиво, сохранив первое появление значения."""

    result = copy.deepcopy(left)
    seen = {canonical_json(value) for value in result}

    for value in right:
        key = canonical_json(value)

        if key in seen:
            continue

        result.append(copy.deepcopy(value))
        seen.add(key)

    return result


def _is_empty(value: Any) -> bool:
    """Проверить, отсутствует ли содержательное значение поля."""

    return value is None or value == "" or value == []


def _compatible_value(field: str, left: Any, right: Any) -> bool:
    """Сравнить два непустых значения с полевой нормализацией."""

    if field == "doi":
        return normalize_doi(str(left)) == normalize_doi(str(right))

    if field in {"edn", "journal_id"}:
        return str(left).casefold() == str(right).casefold()

    if field in {"title", "journal_title"}:
        return normalize_identity_text(str(left)) == normalize_identity_text(str(right))

    if field == "canonical_url":
        return canonicalize_url(str(left)) == canonicalize_url(str(right))

    return left == right


def _plan_retrieval_ids(plan: ManifestPlan) -> list[str]:
    """Вернуть устойчивый непустой список событий, подтверждающих план."""

    identifiers = sorted(
        {event["retrieval_id"] for event in plan.retrieval_events}
    )

    if not identifiers:
        raise ValueError("Согласование документа требует retrieval_event")

    return identifiers


def _build_identity_conflict_records(
    *,
    work_id: str,
    conflicts: dict[str, tuple[Any, Any]],
    retrieval_ids: list[str],
    timestamp: str,
) -> list[dict[str, Any]]:
    """Материализовать конфликты идентичности для ручной проверки."""

    result: list[dict[str, Any]] = []

    for field in sorted(conflicts):
        existing_value, candidate_value = conflicts[field]
        payload = {
            "work_id": work_id,
            "field": field,
            "existing_value": str(existing_value),
            "candidate_value": str(candidate_value),
            "source_retrieval_ids": retrieval_ids,
        }
        result.append(
            {
                "schema_version": "identity-conflicts-v1",
                "conflict_id": _deterministic_id("identity-conflict", payload),
                "created_at": timestamp,
                **payload,
                "status": "pending",
                "resolution_reason": None,
                "resolved_at": None,
            }
        )

    return result


def _work_update_reason(conflicts: dict[str, tuple[Any, Any]]) -> str:
    """Сформулировать причину ревизии карточки работы."""

    if conflicts:
        fields = ", ".join(sorted(conflicts))
        return f"Обнаружен конфликт идентичности в полях: {fields}."

    return "Добавлены совместимые метаданные и проверенные псевдонимы."


def _next_timestamp(previous: str, candidate: str) -> str:
    """Вернуть детерминированную временную метку строго позже прежней."""

    previous_value = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    candidate_value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    minimum = previous_value + timedelta(microseconds=1)
    selected = max(candidate_value, minimum)

    return selected.isoformat(timespec="microseconds")


def _new_identity_alias_specs(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> list[tuple[str, str]]:
    """Вернуть новые совместимые DOI, EDN или URL как псевдонимы."""

    alias_specs: list[tuple[str, str]] = []
    candidate_doi = normalize_doi(candidate.get("doi"))
    existing_doi = normalize_doi(existing.get("doi"))

    if candidate_doi and not existing_doi:
        alias_specs.append(("doi", candidate_doi))

    candidate_edn = _normalize_edn(candidate.get("edn"))
    existing_edn = _normalize_edn(existing.get("edn"))

    if candidate_edn and not existing_edn:
        alias_specs.append(("edn", candidate_edn))

    candidate_url = canonicalize_url(candidate["canonical_url"])
    existing_url = canonicalize_url(existing["canonical_url"])

    if candidate_url != existing_url:
        alias_specs.append(("canonical_url", candidate_url))

    return alias_specs


def _add_verified_identity_aliases(
    plan: ManifestPlan,
    *,
    alias_specs: list[tuple[str, str]],
    timestamp: str,
    preserved_work_id: str,
) -> None:
    """Добавить историю проверенных поздних идентификаторов."""

    retrieval_event = plan.retrieval_events[0]

    for alias_type, alias_value in alias_specs:
        plan.work_aliases.append(
            _build_alias_record(
                work_id=preserved_work_id,
                alias_type=alias_type,
                alias_value=alias_value,
                timestamp=timestamp,
                evidence_sha256=retrieval_event["response_metadata_sha256"],
                retrieval_id=retrieval_event["retrieval_id"],
            )
        )


def _remap_plan_work_ids(
    plan: ManifestPlan,
    mapping: dict[str, str],
) -> None:
    """Перенести все ссылки плана на сохранённые идентификаторы работ."""

    if not mapping:
        return

    for artifact in plan.artifacts:
        artifact["work_id"] = mapping.get(artifact["work_id"], artifact["work_id"])

    retrieval_id_mapping: dict[str, str] = {}

    for event in plan.retrieval_events:
        if event["request_context_type"] == "work":
            previous_context_id = event["request_context_id"]
            preserved_context_id = mapping.get(
                previous_context_id,
                previous_context_id,
            )

            if preserved_context_id != previous_context_id:
                previous_retrieval_id = event["retrieval_id"]
                event["request_context_id"] = preserved_context_id
                event["retrieval_id"] = _retrieval_event_id(event)
                retrieval_id_mapping[previous_retrieval_id] = event["retrieval_id"]

    for artifact in plan.artifacts:
        for retrieval in artifact.get("retrievals", []):
            retrieval["retrieval_id"] = retrieval_id_mapping.get(
                retrieval["retrieval_id"],
                retrieval["retrieval_id"],
            )

    remapped_aliases: list[dict[str, Any]] = []

    for alias in plan.work_aliases:
        record = copy.deepcopy(alias)
        record["work_id"] = mapping.get(record["work_id"], record["work_id"])
        source_retrieval_id = record.get("source_retrieval_id")

        if source_retrieval_id is not None:
            record["source_retrieval_id"] = retrieval_id_mapping.get(
                source_retrieval_id,
                source_retrieval_id,
            )

        record["alias_record_id"] = _alias_record_id(record)
        remapped_aliases.append(record)

    plan.work_aliases = remapped_aliases

    for conflict in plan.identity_conflicts:
        conflict["work_id"] = mapping.get(conflict["work_id"], conflict["work_id"])
        conflict["source_retrieval_ids"] = sorted(
            {
                retrieval_id_mapping.get(retrieval_id, retrieval_id)
                for retrieval_id in conflict["source_retrieval_ids"]
            }
        )
        conflict["conflict_id"] = _conflict_record_id(conflict)

    plan.retrieval_events = _deduplicate_history(
        plan.retrieval_events,
        primary_field="retrieval_id",
    )


def _alias_record_id(record: dict[str, Any]) -> str:
    """Пересчитать ID записи псевдонима после замены work_id."""

    payload = {
        "work_id": record["work_id"],
        "alias_type": record["alias_type"],
        "alias_value": record["alias_value"],
        "verified_at": record["verified_at"],
        "evidence_sha256": record["evidence_sha256"],
        "source_retrieval_id": record["source_retrieval_id"],
    }

    return _deterministic_id("work-alias", payload)


def _conflict_record_id(record: dict[str, Any]) -> str:
    """Пересчитать ID конфликта после замены work_id."""

    payload = {
        "work_id": record["work_id"],
        "field": record["field"],
        "existing_value": record["existing_value"],
        "candidate_value": record["candidate_value"],
        "source_retrieval_ids": record["source_retrieval_ids"],
    }

    return _deterministic_id("identity-conflict", payload)


def _drop_unmaterialized_alias_records(plan: ManifestPlan) -> None:
    """Не считать конфликтующий кандидат проверенным псевдонимом работы."""

    materialized = {
        work["work_id"]: set(work.get("work_aliases", []))
        for work in plan.works
    }
    plan.work_aliases = [
        record
        for record in plan.work_aliases
        if _alias_identity(record) in materialized.get(record["work_id"], set())
    ]


def _alias_identity(record: dict[str, Any]) -> str:
    """Преобразовать историческую запись в псевдоним текущей карточки."""

    return _alias_identity_value(record["alias_type"], record["alias_value"])


def _alias_identity_value(alias_type: str, alias_value: str) -> str:
    """Преобразовать тип и значение в каноничный псевдоним работы."""

    if alias_type == "doi":
        normalized_doi = normalize_doi(alias_value)

        if normalized_doi is None:
            raise ValueError(f"Некорректный DOI псевдонима: {alias_value!r}")

        return f"doi:{normalized_doi}"

    if alias_type == "canonical_url":
        return f"url:{canonicalize_url(alias_value)}"

    return f"{alias_type}:{alias_value}"


def _reconcile_artifacts(
    plan: ManifestPlan,
    existing_artifacts: list[dict[str, Any]],
) -> None:
    """Объединить повторные события получения тех же байтов одной работы."""

    existing_by_payload = {
        (artifact.get("artifact_id"), artifact["work_id"]): artifact
        for artifact in existing_artifacts
    }
    payload_owners = {
        artifact.get("artifact_id"): artifact["work_id"]
        for artifact in existing_artifacts
        if artifact.get("artifact_id")
    }
    artifact_record_mapping: dict[str, str] = {}

    for index, candidate in enumerate(plan.artifacts):
        artifact_id = candidate.get("artifact_id")
        existing_owner = payload_owners.get(artifact_id)

        if existing_owner is not None and existing_owner != candidate["work_id"]:
            raise ManifestConflictError(
                f"Артефакт {artifact_id!r} уже принадлежит работе "
                f"{existing_owner!r}, а не {candidate['work_id']!r}"
            )

        existing = existing_by_payload.get(
            (artifact_id, candidate["work_id"])
        )

        if existing is None:
            continue

        artifact_record_mapping[candidate["artifact_record_id"]] = existing[
            "artifact_record_id"
        ]
        merged = copy.deepcopy(existing)
        retrievals = _merge_retrievals(
            existing.get("retrievals", []),
            candidate.get("retrievals", []),
        )
        rights_record_ids = _ordered_union(
            existing.get("rights_record_ids", []),
            candidate.get("rights_record_ids", []),
        )
        changed = False

        if retrievals != existing.get("retrievals", []):
            merged["retrievals"] = retrievals
            changed = True

        if rights_record_ids != existing.get("rights_record_ids", []):
            merged["rights_record_ids"] = rights_record_ids
            changed = True

        if changed:
            merged["updated_at"] = _next_timestamp(
                existing["updated_at"],
                candidate["updated_at"],
            )
            plan.artifact_update_reasons[existing["artifact_record_id"]] = (
                "Добавлены новое событие получения и применимые записи прав."
            )

        plan.artifacts[index] = merged

    for event in plan.retrieval_events:
        if event["request_context_type"] == "artifact":
            event["request_context_id"] = artifact_record_mapping.get(
                event["request_context_id"],
                event["request_context_id"],
            )


def _merge_retrievals(
    existing: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Объединить проекции событий и отклонить повторный ID с иными данными."""

    result = copy.deepcopy(existing)
    by_id = {record["retrieval_id"]: record for record in result}

    for record in candidate:
        previous = by_id.get(record["retrieval_id"])

        if previous is not None and previous != record:
            raise ManifestConflictError(
                f"retrieval_id={record['retrieval_id']!r} имеет разные проекции"
            )

        if previous is None:
            result.append(copy.deepcopy(record))
            by_id[record["retrieval_id"]] = record

    return sorted(result, key=lambda record: record["retrieval_id"])


def _deduplicate_history(
    records: list[dict[str, Any]],
    *,
    primary_field: str,
) -> list[dict[str, Any]]:
    """Убрать точные повторы исторических записей и выявить конфликт ID."""

    result: dict[str, dict[str, Any]] = {}

    for record in records:
        record_id = record[primary_field]
        previous = result.get(record_id)

        if previous is not None and previous != record:
            raise ManifestConflictError(
                f"{primary_field}={record_id!r} назначен разным записям"
            )

        result[record_id] = record

    return list(result.values())
