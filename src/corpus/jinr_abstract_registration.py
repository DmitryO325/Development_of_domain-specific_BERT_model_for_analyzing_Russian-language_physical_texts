"""Офлайн-регистрация тезисов ОИЯИ с сохранением исходного типа Book chapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jinr_abstract_input import read_abstract_batch
from .jinr_article_input import VerifiedArticle
from .jinr_article_registration import (
    _build_repository_work,
    _plan_verified_batch,
    _validate_options,
)
from .jinr_registration import _metadata_values, reconcile_preprint_plan
from .manifests import ManifestConflictError, ManifestPlan, ManifestStore
from .profiles import get_source_profile
from .registration import RegistrationOptions


def plan_abstract_batch(
    project_root: Path,
    path: Path,
    options: RegistrationOptions,
    collected_at: str | None = None,
    *,
    metadata_rights_record_ids: tuple[str, ...],
) -> ManifestPlan:
    """Проверить разрешённый пакет тезисов и подготовить план без записи и сети."""

    _validate_options(options)
    batch = read_abstract_batch(project_root, path)

    return _plan_verified_batch(
        batch, options, collected_at,
        metadata_rights_record_ids=metadata_rights_record_ids,
        profile=get_source_profile("jinr_abstracts"),
        work_builder=_build_abstract_work,
    )


def reconcile_abstract_plan(
    store: ManifestStore,
    plan: ManifestPlan,
) -> tuple[ManifestPlan, dict[str, str]]:
    """Сохранить даты повторного импорта и остановить пакет при конфликте работ."""

    reconciled, expected_hashes = reconcile_preprint_plan(store, plan)

    if reconciled.identity_conflicts:
        raise ManifestConflictError(
            "Обнаружен конфликт с существующими работами; регистрация тезисов остановлена"
        )

    return reconciled, expected_hashes


def _build_abstract_work(
    article: VerifiedArticle,
    timestamp: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Сохранить исходный сборник и Book chapter отдельно от журнального издания."""

    entry = article.entry
    item = article.item

    if entry.get("genre") != "conference_abstract":
        raise ValueError("Тезисы должны иметь genre='conference_abstract'")

    if entry.get("journal_id") is not None:
        raise ValueError("Тезисам нельзя назначать journal_id")

    document_types = _metadata_values(item, "dc.type")

    if document_types != ["Book chapter"]:
        raise ValueError("Исходный тип тезисов должен быть Book chapter")

    collections = _metadata_values(item, "dc.relation.ispartof")

    if len(collections) != 1 or not collections[0].strip():
        raise ValueError("У тезисов нет однозначного названия сборника")

    work = _build_repository_work(
        article, timestamp, warnings,
        profile=get_source_profile("jinr_abstracts"),
        genre="conference_abstract", journal_id=None, journal_title=None,
    )
    work["collection_title"] = collections[0].strip()
    work["source_document_type"] = document_types[0]

    return work
