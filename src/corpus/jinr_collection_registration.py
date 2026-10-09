"""Офлайн-регистрация полнотекстовых материалов научных сборников ОИЯИ."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jinr_article_input import VerifiedArticle
from .jinr_article_registration import (
    _build_repository_work,
    _plan_verified_batch,
    _validate_options,
)
from .jinr_collection_input import read_collection_batch
from .jinr_registration import _metadata_values, reconcile_preprint_plan
from .manifests import ManifestConflictError, ManifestPlan, ManifestStore
from .profiles import get_source_profile
from .registration import RegistrationOptions


def plan_collection_batch(
    project_root: Path,
    path: Path,
    options: RegistrationOptions,
    collected_at: str | None = None,
    *,
    metadata_rights_record_ids: tuple[str, ...],
) -> ManifestPlan:
    """Проверить полные материалы сборников и подготовить план без записи и сети."""

    _validate_options(options)
    batch = read_collection_batch(project_root, path)

    return _plan_verified_batch(
        batch, options, collected_at,
        metadata_rights_record_ids=metadata_rights_record_ids,
        profile=get_source_profile("jinr_collections"),
        work_builder=_build_collection_work,
    )


def reconcile_collection_plan(
    store: ManifestStore,
    plan: ManifestPlan,
) -> tuple[ManifestPlan, dict[str, str]]:
    """Сохранить даты повторного импорта и остановить пакет при конфликте работ."""

    reconciled, expected_hashes = reconcile_preprint_plan(store, plan)

    if reconciled.identity_conflicts:
        raise ManifestConflictError(
            "Обнаружен конфликт с существующими работами; регистрация сборников остановлена"
        )

    return reconciled, expected_hashes


def _build_collection_work(
    article: VerifiedArticle,
    timestamp: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Сохранить название сборника отдельно от журнала и исходный тип Book chapter."""

    entry = article.entry
    item = article.item

    if entry.get("genre") != "collection_paper":
        raise ValueError("Полный материал сборника должен иметь genre='collection_paper'")

    if entry.get("journal_id") is not None:
        raise ValueError("Материалу сборника нельзя назначать journal_id")

    document_types = _metadata_values(item, "dc.type")

    if document_types != ["Book chapter"]:
        raise ValueError("Исходный тип материала сборника должен быть Book chapter")

    collections = _metadata_values(item, "dc.relation.ispartof")

    if len(collections) != 1 or not collections[0].strip():
        raise ValueError("У материала нет однозначного названия научного сборника")

    work = _build_repository_work(
        article, timestamp, warnings,
        profile=get_source_profile("jinr_collections"),
        genre="collection_paper", journal_id=None, journal_title=None,
    )
    work["collection_title"] = collections[0].strip()
    work["source_document_type"] = document_types[0]

    return work
