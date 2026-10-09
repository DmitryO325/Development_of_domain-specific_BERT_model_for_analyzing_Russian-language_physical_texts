"""Планы офлайн-регистрации журнальных статей ОИЯИ с ручными PDF."""

from __future__ import annotations

import re

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import resolve_work_identity
from .jinr_article_input import (
    ArticleBatch,
    VerifiedArticle,
    _article_title,
    _corpus_group,
    read_article_batch,
)
from .jinr_registration import (
    _build_alias,
    _build_event,
    _deterministic_id,
    _item_doi,
    _metadata_values,
    _publication_date,
    _timestamp,
    reconcile_preprint_plan,
)
from .local_registration import (
    _build_artifact,
    _manual_provenance_sha256,
    _retrieval_projection,
)
from .manifests import ManifestConflictError, ManifestPlan, ManifestStore
from .profiles import SourceProfile, get_source_profile
from .registration import GENRES, RegistrationOptions


def plan_article_batch(
    project_root: Path,
    path: Path,
    options: RegistrationOptions,
    collected_at: str | None = None,
    *,
    metadata_rights_record_ids: tuple[str, ...],
) -> ManifestPlan:
    """Проверить входной пакет и построить план без записи, сети и допуска к обучению."""

    _validate_options(options)
    batch = read_article_batch(project_root, path)

    return _plan_verified_batch(
        batch, options, collected_at,
        metadata_rights_record_ids=metadata_rights_record_ids,
        profile=get_source_profile("jinr_articles"),
        work_builder=_build_article_work,
    )


def _validate_options(options: RegistrationOptions) -> None:
    """Ограничить локальную регистрацию ручными PDF без смены способа получения."""

    for field_name, expected in {
        "content_role": "full_text",
        "acquisition_method": "manual_download",
        "acquisition_scope": "bulk",
        "response_representation": "pdf",
        "request_context_type": "work",
    }.items():
        if getattr(options, field_name) != expected:
            raise ValueError(f"options.{field_name} должен быть {expected!r}")


def _plan_verified_batch(
    batch: ArticleBatch,
    options: RegistrationOptions,
    collected_at: str | None,
    *,
    metadata_rights_record_ids: tuple[str, ...],
    profile: SourceProfile,
    work_builder: Callable[[VerifiedArticle, str, list[str]], dict[str, Any]],
) -> ManifestPlan:
    """Собрать проверенные локальные PDF и происхождение независимо от вида издания."""

    _validate_options(options)
    pdf_rights = _rights_ids(options.rights_record_ids, "PDF")
    metadata_rights = _rights_ids(metadata_rights_record_ids, "метаданных API")
    timestamp = _timestamp(collected_at) if collected_at is not None else (
        datetime.now(timezone.utc).isoformat(timespec="microseconds")
    )
    plan = ManifestPlan()
    metadata_events: dict[str, dict[str, Any]] = {}
    work_ids: set[str] = set()
    journal_names: dict[str, str] = {}
    journal_ids: dict[str, str] = {}

    for article in batch.entries:
        work = work_builder(article, timestamp, plan.warnings)

        if work["work_id"] in work_ids:
            raise ValueError(f"Несколько статей пакета имеют один work_id: {work['work_id']}")

        work_ids.add(work["work_id"])

        if work["journal_id"] is not None:
            _check_journal_mapping(work, journal_names, journal_ids)

        source = article.metadata_response
        event = _build_event(
            source,
            context_type="source",
            context_id=profile.source_id,
            source_group_id=profile.source_group_id,
            method="api",
            rights_record_ids=metadata_rights,
            timestamp=timestamp,
        )
        previous_event = metadata_events.get(event["retrieval_id"])

        if previous_event is not None and previous_event != event:
            raise ValueError("В пакете противоречивые описания одного события API")

        metadata_events[event["retrieval_id"]] = event
        aliases = [_build_alias(
            work["work_id"], "source_native_id", f"{profile.source_id}:{article.entry['item_uuid']}",
            source, event, timestamp,
        )]

        for alias_type in ("doi", "edn"):
            if work[alias_type] is not None:
                aliases.append(_build_alias(
                    work["work_id"], alias_type, work[alias_type], source, event, timestamp,
                ))

        work["work_aliases"] = [f"{alias['alias_type']}:{alias['alias_value']}" for alias in aliases]
        manual_events = [
            _manual_event(article, acquisition, work["work_id"], pdf_rights, timestamp)
            for acquisition in article.acquisitions
        ]
        artifact = _build_artifact(
            artifact_record_id=f"artifact-record:sha256:{article.pdf_sha256}",
            artifact_id=f"sha256:{article.pdf_sha256}",
            work_id=work["work_id"],
            retrieval=_retrieval_projection(manual_events[0]),
            rights_record_ids=list(pdf_rights),
            relative_path=article.pdf_path,
            digest=article.pdf_sha256,
            byte_count=article.pdf_bytes,
            timestamp=timestamp,
        )
        # Одинаковые байты остаются одним артефактом, обе истории получения сохраняются.
        artifact["acquisition_scope"] = "bulk"
        artifact["retrievals"] = [_retrieval_projection(item) for item in manual_events]
        plan.works.append(work)
        plan.artifacts.append(artifact)
        plan.retrieval_events.extend(manual_events)
        plan.work_aliases.extend(aliases)

    plan.retrieval_events.extend(metadata_events.values())
    plan.warnings.append(
        "Связь PDF и публикации подтверждена локальными свидетельствами, "
        "а не серверной цепочкой bitstream→item. Допуск к обучению не назначен."
    )

    return plan


def reconcile_article_plan(
    store: ManifestStore,
    plan: ManifestPlan,
) -> tuple[ManifestPlan, dict[str, str]]:
    """Сохранить даты повторного импорта и остановить запись при конфликте работ."""

    # Общая часть согласования не зависит от жанра: она сохраняет даты событий и псевдонимов.
    reconciled, expected_hashes = reconcile_preprint_plan(store, plan)

    if reconciled.identity_conflicts:
        raise ManifestConflictError(
            "Обнаружен конфликт с существующими работами; регистрация статей остановлена"
        )

    return reconciled, expected_hashes


def _rights_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Проверить явные ID прав, не подставляя разрешения препринтов или других источников."""

    if not isinstance(values, tuple) or not values or any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in values
    ):
        raise ValueError(f"Для {label} нужны непустые явные идентификаторы записей прав")

    return tuple(sorted(set(values)))


def _build_article_work(
    article: VerifiedArticle,
    timestamp: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Сохранить метаданные API и явно назначенные журнал и поджанр без догадок."""

    entry = article.entry
    item = article.item
    genre = entry.get("genre")
    journal_id = entry.get("journal_id")

    if not isinstance(genre, str) or genre not in GENRES:
        raise ValueError(f"Для статьи {entry['item_uuid']} нужно явно заполнить genre")

    if not isinstance(journal_id, str) or not journal_id.strip() or journal_id != journal_id.strip():
        raise ValueError(f"Для статьи {entry['item_uuid']} нужно явно заполнить journal_id")

    journals = _metadata_values(item, "dc.relation.ispartof")

    if len(journals) != 1:
        raise ValueError(f"У статьи {entry['item_uuid']} нет однозначного названия журнала")

    return _build_repository_work(
        article, timestamp, warnings,
        profile=get_source_profile("jinr_articles"),
        genre=genre,
        journal_id=journal_id,
        journal_title=journals[0].strip(),
    )


def _build_repository_work(
    article: VerifiedArticle,
    timestamp: str,
    warnings: list[str],
    *,
    profile: SourceProfile,
    genre: str,
    journal_id: str | None,
    journal_title: str | None,
) -> dict[str, Any]:
    """Сохранить общие метаданные проверенной карточки без выдуманной даты и допуска."""

    entry = article.entry
    item = article.item
    title = _article_title(item).strip()
    authors = _metadata_values(item, "dc.contributor.author")
    published_at, published_year = _publication_date(item)
    doi = _item_doi(item)
    identity = resolve_work_identity(
        source_id=profile.source_id,
        title=title,
        authors=authors,
        year=published_year,
        doi=doi,
        native_id=entry["item_uuid"],
    )
    abstracts = _metadata_values(item, "dc.description.abstract")

    work = {
        "schema_version": "works-v1",
        "created_at": timestamp,
        "work_id": identity.work_id,
        "work_aliases": [],
        "source_group_id": profile.source_group_id,
        "source_id": profile.source_id,
        "platform": profile.platform,
        "journal_id": journal_id,
        "journal_title": journal_title,
        "canonical_url": item["_links"]["self"]["href"],
        "doi": doi,
        "edn": _article_edn(item, warnings),
        "identity_confidence": identity.confidence,
        "title": title,
        "authors": authors,
        "abstract": "\n\n".join(abstracts) or None,
        "keywords": _metadata_values(item, "dc.subject"),
        "published_at": published_at,
        "published_year": published_year,
        "language": "ru",
        "genre": genre,
        "pacs_codes_raw": [],
        "udc_codes_raw": [],
        "publisher_section": None,
        "duplicate_of_work_id": None,
        "eligibility_status": "pending",
        "exclusion_reason": None,
        "updated_at": timestamp,
    }
    corpus_group = _corpus_group(entry, _metadata_values(item, "dc.type")[0])

    if corpus_group is not None:
        work["corpus_group_id"] = corpus_group

    return work


def _article_edn(item: dict[str, Any], warnings: list[str]) -> str | None:
    """Прочитать однозначный EDN из API и предупредить о нескольких корректных кодах."""

    values = {value.strip().upper() for value in _metadata_values(item, "local.EDN")}

    if not values:
        return

    if any(re.fullmatch(r"[A-Z]{6}", value) is None for value in values):
        raise ValueError("Карточка статьи содержит неверный EDN")

    if len(values) > 1:
        warnings.append(
            f"Карточка статьи {item['uuid']} содержит неоднозначные EDN: "
            f"{', '.join(sorted(values))}. Поле edn оставлено пустым; "
            "псевдоним EDN не создаётся."
        )

        return

    return next(iter(values))


def _check_journal_mapping(
    work: dict[str, Any],
    names: dict[str, str],
    identifiers: dict[str, str],
) -> None:
    """Отклонить противоречивое соответствие названий журналов и их ID внутри пакета."""

    journal_id = work["journal_id"]
    normalized_title = " ".join(work["journal_title"].casefold().split())

    if journal_id in names and names[journal_id] != normalized_title:
        raise ValueError(f"journal_id={journal_id!r} сопоставлен разным журналам")

    if normalized_title in identifiers and identifiers[normalized_title] != journal_id:
        raise ValueError("Одному названию журнала в пакете назначены разные journal_id")

    names[journal_id] = normalized_title
    identifiers[normalized_title] = journal_id


def _manual_event(
    article: VerifiedArticle,
    acquisition: dict[str, Any],
    work_id: str,
    rights: tuple[str, ...],
    timestamp: str,
) -> dict[str, Any]:
    """Сохранить факт ручного получения без HTTP-статуса и заголовков браузера."""

    source_url = acquisition["source_url"]
    retrieved_at = acquisition["retrieved_at"]
    acquisition_agent = acquisition["acquisition_agent"]
    provenance_sha256 = _manual_provenance_sha256(
        source_url=source_url,
        retrieved_at=retrieved_at,
        acquisition_agent=acquisition_agent,
    )
    core = {
        "request_context_type": "work",
        "request_context_id": work_id,
        "source_group_id": None,
        "requested_url": source_url,
        "final_url": None,
        "retrieved_at": retrieved_at,
        "acquisition_method": "manual_download",
        "acquisition_scope": "bulk",
        "rights_record_ids": list(rights),
        "http_status": None,
        "response_headers": {},
        "response_metadata_sha256": provenance_sha256,
        "response_sha256": article.pdf_sha256,
        "response_bytes": article.pdf_bytes,
        "outcome": "succeeded",
    }

    return {
        "schema_version": "retrieval-events-v1",
        "retrieval_id": _deterministic_id("retrieval", core),
        "created_at": timestamp,
        **core,
        "acquisition_agent": acquisition_agent,
        "response_path": acquisition["staged_relative_path"],
        "error_code": None,
        "error_detail": None,
    }
