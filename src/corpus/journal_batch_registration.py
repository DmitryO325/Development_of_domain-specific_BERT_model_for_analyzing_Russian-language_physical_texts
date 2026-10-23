"""Регистрация проверенной партии с узкими основаниями сбора и хранения."""

from __future__ import annotations

import json

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .journal_batch_input import FrozenJournalBatch
from .journal_batch_constraints import ensure_no_conflicting_rights
from .journal_batch_rights import attach_batch_rights, batch_response_rights_ids
from .journal_registration import combine_article_plans, plan_collected_article
from .manifests import ManifestConflictError, ManifestPlan, ManifestStore
from .registration import reconcile_document_plan


def read_batch_assessment(path: Path, batch: FrozenJournalBatch) -> dict[str, Any]:
    """Прочитать явную проверку оснований именно для выбранного состава партии."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("Проверка оснований должна быть обычным локальным JSON-файлом")

    with path.open("rb") as assessment_file:
        content = assessment_file.read(4 * 1024 * 1024 + 1)

    if len(content) > 4 * 1024 * 1024:
        raise ValueError("Файл проверки оснований слишком большой")

    assessment = json.loads(content)

    if not isinstance(assessment, dict) or assessment.get("prepared_batch_sha256") != batch.sha256:
        raise ValueError("Проверка оснований относится к другому составу партии")

    policy = assessment.get("policy")

    if not isinstance(policy, dict) or policy.get("snapshot") != batch.raw_packet["policy"]["dec012_snapshot"]:
        raise ValueError("Политика в проверке оснований отличается от снимка партии")

    return assessment


def _registration_timestamp(store: ManifestStore, batch: FrozenJournalBatch) -> str:
    """Сохранить первую дату атомарного импорта при точном повторе той же партии."""

    identifiers: set[str] = set()

    for article in batch.records:
        responses = [article.card_response]

        if article.pdf_response is not None:
            responses.append(article.pdf_response)

        for response in responses:
            identifiers.update(batch_response_rights_ids(batch.batch_id, response))

    existing = [record for record in store.records("rights") if record["rights_record_id"] in identifiers]

    if not existing:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    timestamps = {record["created_at"] for record in existing}

    if len(existing) != len(identifiers) or len(timestamps) != 1:
        raise ManifestConflictError("Основания партии уже записаны частично или с разными датами; нужна проверка")

    return timestamps.pop()


def reconcile_batch_registration(
    store: ManifestStore,
    batch: FrozenJournalBatch,
    assessment: dict[str, Any],
) -> tuple[ManifestPlan, dict[str, str]]:
    """Подготовить атомарный импорт без сети, расширения прав и изменения допуска."""

    if assessment.get("prepared_batch_sha256") != batch.sha256:
        raise ValueError("Основания не подтверждают контрольную сумму выбранной партии")

    policy = assessment.get("policy")

    if not isinstance(policy, dict) or policy.get("snapshot") != batch.raw_packet["policy"]["dec012_snapshot"]:
        raise ValueError("Политика в проверке оснований отличается от снимка партии")

    # Общий снимок фиксируется до чтения существующих оснований: параллельная
    # запись должна приводить к повторной проверке, а не смешению двух версий.
    expected_hashes = store.snapshot_hashes()
    registered_at = _registration_timestamp(store, batch)
    plans = []

    for article in batch.records:
        pdf_ids = None

        if article.pdf_response is not None:
            pdf_ids = batch_response_rights_ids(batch.batch_id, article.pdf_response)

        plans.append(plan_collected_article(
            article,
            rights_record_ids=batch_response_rights_ids(batch.batch_id, article.card_response),
            pdf_rights_record_ids=pdf_ids,
            registered_at=registered_at,
            scoped_to_response=True,
        ))

    reconciled, _ = reconcile_document_plan(store, combine_article_plans(plans))

    if reconciled.identity_conflicts:
        raise ManifestConflictError("В партии обнаружен конфликт идентичности; рабочие реестры не изменены")

    existing_works = {work["work_id"]: work for work in store.records("works")}

    for work in reconciled.works:
        previous = existing_works.get(work["work_id"])

        if previous is None:
            if work["eligibility_status"] != "pending":
                raise ManifestConflictError("Новая работа должна оставаться без допуска к обучению")

        elif work["eligibility_status"] != previous["eligibility_status"]:
            raise ManifestConflictError("Импорт не должен менять ранее установленный допуск")

    # При повторе сохраняем первую дату регистрации, но новые ограничения
    # проверяем на настоящий момент, а не по прежнему состоянию знаний.
    ensure_no_conflicting_rights(store, reconciled, datetime.now(timezone.utc).isoformat())

    return attach_batch_rights(
        reconciled, batch_id=batch.batch_id, registered_at=registered_at, evidence=assessment,
    ), expected_hashes
