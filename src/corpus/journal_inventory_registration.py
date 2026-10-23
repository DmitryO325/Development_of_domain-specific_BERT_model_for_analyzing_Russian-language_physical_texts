"""Безопасное согласование инвентарных партий без создания прав и записи реестров."""

from __future__ import annotations

import copy

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .journal_import import CollectedArticle
from .journal_inventory_input import _inside
from .journal_registration import combine_article_plans, plan_collected_article
from .manifests import ManifestConcurrencyError, ManifestConflictError, ManifestPlan, ManifestStore, sha256_file
from .registration import _alias_record_id, _normalize_timestamp, reconcile_document_plan

RESPONSE_FIELDS = (
    "requested_url", "final_url", "retrieved_at", "response_metadata_sha256", "response_sha256",
)
SNAPSHOT_KINDS = ("works", "artifacts", "retrieval_events", "work_aliases", "rights")


def deferred_source_report(
    descriptors: tuple[dict[str, Any], ...] | list[dict[str, Any]], project_root: Path,
) -> list[dict[str, Any]]:
    """Включить проверенные конфликты исходников в отчёт, не создавая работы или артефакты."""

    root = Path(project_root).resolve()

    return [
        {
            "status": "deferred", "reason": descriptor["reason"], "source": descriptor["source"],
            "url": descriptor["details"]["article_url"],
            "path": str(_inside(root, descriptor["record_path"])),
        }
        for descriptor in descriptors
    ]


class _InventorySnapshot(ManifestStore):
    """Держать один проверенный снимок записей для чистого согласования партии."""

    def __init__(self, store: ManifestStore) -> None:
        """Прочитать реестры однократно и отвергнуть их конкурентное изменение."""

        super().__init__(
            project_root=store.project_root, manifest_dir=store.manifest_dir, schema_dir=store.schema_dir,
        )
        self.hashes = store.snapshot_hashes()
        self.history_hashes = _history_hashes(store)
        self.saved_records = {kind: store.records(kind) for kind in SNAPSHOT_KINDS}
        self.ensure_current(store)
        self.works = {record["work_id"]: record for record in self.saved_records["works"]}
        self.artifacts = {
            record["artifact_record_id"]: record for record in self.saved_records["artifacts"]
        }
        self.rights = {record["rights_record_id"]: record for record in self.saved_records["rights"]}
        self.by_id = {
            kind: {record[primary_field]: record for record in self.saved_records[kind]}
            for kind, primary_field in (
                ("works", "work_id"), ("artifacts", "artifact_record_id"),
                ("retrieval_events", "retrieval_id"), ("work_aliases", "alias_record_id"),
            )
        }
        self.responses: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)

        for event in self.saved_records["retrieval_events"]:
            self.responses[_response_key(event)].append(event)

    def records(self, kind: str) -> list[dict[str, Any]]:
        """Вернуть записи замороженного снимка для согласования без повторного чтения."""

        return list(self.saved_records[kind])

    def snapshot_hashes(self) -> dict[str, str]:
        """Вернуть CAS-хеши того снимка, на котором построен план."""

        return dict(self.hashes)

    def ensure_current(self, store: ManifestStore) -> None:
        """Остановиться, если рабочие реестры изменились во время подготовки."""

        if store.snapshot_hashes() != self.hashes or _history_hashes(store) != self.history_hashes:
            raise ManifestConcurrencyError("Реестры изменились во время инвентарной сверки; повторите подготовку")


def _history_hashes(store: ManifestStore) -> dict[str, str | None]:
    """Защитить прочитанную историю прав и происхождения сверх CAS текущих снимков."""

    result = {}

    for kind in ("rights", "retrieval_events", "work_aliases"):
        path = store.path_for(kind)
        result[kind] = sha256_file(path) if path.exists() else None

    return result


@dataclass(frozen=True)
class _Candidate:
    """Хранить исходный план и разрешённую идентичность одной проверенной карточки."""

    article: CollectedArticle
    plan: ManifestPlan
    work: dict[str, Any]
    already_registered: bool


def reconcile_inventory_registration(
    store: ManifestStore,
    articles: tuple[CollectedArticle, ...] | list[CollectedArticle],
    registered_at: str,
) -> tuple[ManifestPlan, dict[str, str], list[dict[str, Any]]]:
    """Подготовить инвентарный план и отчёт об отложенных карточках, ничего не записывая."""

    if not isinstance(store, ManifestStore):
        raise ValueError("Для сверки необходимо хранилище ManifestStore")

    if not isinstance(articles, (tuple, list)) or any(not isinstance(article, CollectedArticle) for article in articles):
        raise ValueError("Статьи должны быть списком или кортежем проверенных CollectedArticle")

    timestamp = _normalize_timestamp(registered_at, field_name="registered_at")
    snapshot = _InventorySnapshot(store)
    candidates = []
    report: list[dict[str, Any]] = []

    for article in articles:
        try:
            candidate = _prepare_candidate(snapshot, article, timestamp)

        except (ManifestConflictError, ValueError) as exception:
            report.append(_report(article, "deferred", str(exception)))
            continue

        if candidate.already_registered:
            report.append(_report(article, "already_registered", "Эта карточка и точные HTTP-ответы уже учтены"))

        else:
            candidates.append(candidate)

    safe_candidates = _exclude_batch_conflicts(candidates, report)
    combined = combine_article_plans([candidate.plan for candidate in safe_candidates])
    reconciled, hashes = reconcile_document_plan(snapshot, combined)
    _require_compatible_plan(snapshot, reconciled)
    referenced_paths = {
        *(artifact.get("path") for artifact in reconciled.artifacts),
        *(event.get("response_path") for event in reconciled.retrieval_events),
    }
    reconciled.blobs = [blob for blob in reconciled.blobs if blob.relative_path in referenced_paths]
    snapshot.ensure_current(store)

    return reconciled, hashes, report


def _prepare_candidate(
    snapshot: _InventorySnapshot,
    article: CollectedArticle,
    registered_at: str,
) -> _Candidate:
    """Установить контекст, выбрать существующие основания и восстановить устойчивые ID."""

    initial = plan_collected_article(
        article, rights_record_ids=(), registered_at=registered_at,
        scoped_to_response=True, inventory_only=True,
    )
    resolved, _ = reconcile_document_plan(snapshot, initial)
    _require_compatible_plan(snapshot, resolved)
    work = resolved.works[0]
    rights_by_response = _event_rights(snapshot, resolved, registered_at)
    card_key = _response_key(initial.retrieval_events[0])
    pdf_rights: tuple[str, ...] = ()

    if article.pdf_response is not None:
        pdf_key = _response_key(initial.retrieval_events[-1])
        pdf_rights = rights_by_response[pdf_key]

    plan = plan_collected_article(
        article, rights_record_ids=rights_by_response[card_key], pdf_rights_record_ids=pdf_rights,
        registered_at=registered_at, scoped_to_response=True, inventory_only=True,
    )
    _attach_storage_rights(snapshot, plan, resolved, registered_at)
    _reuse_existing_responses(snapshot, plan, work)
    checked, _ = reconcile_document_plan(snapshot, plan)
    _require_compatible_plan(snapshot, checked)

    return _Candidate(article, plan, checked.works[0], _plan_is_unchanged(snapshot, checked))


def _plan_is_unchanged(snapshot: _InventorySnapshot, plan: ManifestPlan) -> bool:
    """Проверить точное совпадение согласованного результата с сохранёнными записями."""

    for kind, primary_field in (
        ("works", "work_id"), ("artifacts", "artifact_record_id"),
        ("retrieval_events", "retrieval_id"), ("work_aliases", "alias_record_id"),
    ):
        existing = snapshot.by_id[kind]

        if any(record != existing.get(record[primary_field]) for record in getattr(plan, kind)):
            return False

    return True


def _require_compatible_plan(snapshot: _InventorySnapshot, plan: ManifestPlan) -> None:
    """Не допустить скрытого конфликта идентичности или изменения прежнего допуска."""

    if plan.identity_conflicts:
        fields = sorted({conflict["field"] for conflict in plan.identity_conflicts})
        raise ManifestConflictError("Требуется ручное разрешение конфликта идентичности: " + ", ".join(fields))

    for work in plan.works:
        existing = snapshot.works.get(work["work_id"])

        if existing is not None and any(
            work.get(field) != existing.get(field) for field in ("eligibility_status", "exclusion_reason")
        ):
            raise ManifestConflictError("Инвентарная сверка не должна менять прежний допуск работы")


def _response_key(event: dict[str, Any]) -> tuple[str, ...]:
    """Идентифицировать конкретный HTTP-ответ, а не статью или одни байты файла."""

    values = []

    for field in RESPONSE_FIELDS:
        value = event.get(field)

        if field == "retrieved_at" and isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(
                timespec="microseconds",
            )

        values.append(str(value))

    return tuple(values)


def _event_rights(
    snapshot: _InventorySnapshot,
    plan: ManifestPlan,
    registered_at: str,
) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Выбрать все определяющие права на момент получения, включая запрещающие."""

    works = {**snapshot.works, **{work["work_id"]: work for work in plan.works}}
    artifacts = {
        **snapshot.artifacts, **{artifact["artifact_record_id"]: artifact for artifact in plan.artifacts},
    }
    known_at = datetime.fromisoformat(registered_at)
    result = {}

    for event in plan.retrieval_events:
        work, artifact = snapshot._event_context(event, works, artifacts)
        retrieved_at = datetime.fromisoformat(event["retrieved_at"].replace("Z", "+00:00"))
        source_groups = snapshot._event_source_group_ids(event)
        applicable = [
            right for right in snapshot._rights_active_at(snapshot.rights, retrieved_at, known_at=known_at)
            if snapshot._right_applies_to_event(right, event, work, artifact, source_group_ids=source_groups)
        ]
        identifiers = {
            right["rights_record_id"]
            for operation in ("acquisition", "storage")
            for right in snapshot._event_operation_rights(applicable, operation=operation, event=event)
        }
        result[_response_key(event)] = tuple(sorted(identifiers))

    return result


def _attach_storage_rights(
    snapshot: _InventorySnapshot,
    plan: ManifestPlan,
    resolved: ManifestPlan,
    registered_at: str,
) -> None:
    """Добавить к новым артефактам текущие основания хранения без изменения событий."""

    at = datetime.fromisoformat(registered_at)
    active = snapshot._rights_active_at(snapshot.rights, at, known_at=at)
    resolved_artifacts = {artifact["artifact_id"]: artifact for artifact in resolved.artifacts}
    work = resolved.works[0]

    for artifact in plan.artifacts:
        context = resolved_artifacts[artifact["artifact_id"]]
        applicable = [right for right in active if snapshot._rights_apply(right, work, context)]
        storage = snapshot._operation_decisions(applicable, operation="storage", artifact=context)
        artifact["rights_record_ids"] = sorted({
            *artifact["rights_record_ids"], *(right["rights_record_id"] for right in storage),
        })


def _reuse_existing_responses(
    snapshot: _InventorySnapshot,
    plan: ManifestPlan,
    work: dict[str, Any],
) -> None:
    """Заменить повторный факт прежним событием, сохранив его outcome, права и контекст."""

    mapping = {}
    events = []

    for event in plan.retrieval_events:
        previous = snapshot.responses.get(_response_key(event), [])

        if not previous:
            events.append(event)
            continue

        for existing in previous:
            _require_event_context(snapshot, existing, work)

        # Ранее один ответ мог получить несколько исторических ID. Нового
        # события не создаём; выбор самого раннего воспроизводим и не меняет историю.
        existing = min(previous, key=lambda record: (record["created_at"], record["retrieval_id"]))
        mapping[event["retrieval_id"]] = existing
        events.append(copy.deepcopy(existing))

        for artifact in plan.artifacts:
            if (
                any(item["retrieval_id"] == event["retrieval_id"] for item in artifact["retrievals"])
                and existing["outcome"] != "recorded"
                and artifact["artifact_record_id"] not in snapshot.artifacts
            ):
                raise ManifestConflictError("Прежний ответ не материализован как артефакт; нужна отдельная проверка")

    plan.retrieval_events = events

    for artifact in plan.artifacts:
        for retrieval in artifact["retrievals"]:
            previous = mapping.get(retrieval["retrieval_id"])

            if previous is not None:
                retrieval.update({
                    "retrieval_id": previous["retrieval_id"], "retrieved_url": previous["final_url"],
                    "retrieved_at": previous["retrieved_at"],
                    "response_metadata_sha256": previous["response_metadata_sha256"],
                })

    for alias in plan.work_aliases:
        identifier = alias.get("source_retrieval_id")
        previous = mapping.get(identifier)
        alias["source_retrieval_id"] = previous["retrieval_id"] if previous is not None else identifier
        alias["alias_record_id"] = _alias_record_id(alias)


def _require_event_context(
    snapshot: _InventorySnapshot,
    event: dict[str, Any],
    work: dict[str, Any],
) -> None:
    """Не переиспользовать HTTP-событие чужой работы, кроме общего оглавления источника."""

    if event["request_context_type"] == "source":
        if (
            event["request_context_id"] != work["source_id"]
            or event.get("source_group_id") != work["source_group_id"]
        ):
            raise ManifestConflictError("Точный HTTP-ответ уже связан с другим источником")

        return

    previous_work, _ = snapshot._event_context(event, snapshot.works, snapshot.artifacts)

    if previous_work is None or previous_work["work_id"] != work["work_id"]:
        raise ManifestConflictError("Точный HTTP-ответ уже связан с другой работой или имеет неполный контекст")


def _exclude_batch_conflicts(candidates: list[_Candidate], report: list[dict[str, Any]]) -> list[_Candidate]:
    """Отложить неоднозначные пересечения работ и файлов внутри ещё не записанной партии."""

    groups: dict[tuple[str, str], set[int]] = defaultdict(set)

    for index, candidate in enumerate(candidates):
        for field in ("work_id", "doi", "edn", "canonical_url"):
            value = candidate.work.get(field)

            if value:
                groups[(field, str(value))].add(index)

        for artifact in candidate.plan.artifacts:
            groups[("artifact_id", artifact["artifact_id"])].add(index)

    conflicts = set()
    duplicates = set()

    for indices in groups.values():
        if len(indices) < 2:
            continue

        ordered = sorted(indices)
        first = candidates[ordered[0]]

        if all(candidate.plan == first.plan for candidate in (candidates[index] for index in ordered[1:])):
            duplicates.update(ordered[1:])

        else:
            conflicts.update(indices)

    result = []

    for index, candidate in enumerate(candidates):
        if index in conflicts:
            report.append(_report(
                candidate.article, "deferred", "Несколько карточек партии претендуют на одну работу или один файл",
            ))

        elif index in duplicates:
            report.append(_report(candidate.article, "skipped", "Точный повтор уже включённой карточки партии"))

        else:
            result.append(candidate)

    return result


def _report(article: CollectedArticle, status: str, reason: str) -> dict[str, Any]:
    """Сформировать адресную запись отчёта без изменения проверенной карточки."""

    return {
        "status": status, "reason": reason, "source": article.document.source,
        "url": article.document.url, "path": str(article.record_path),
    }
