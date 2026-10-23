"""Защита импорта партии от перекрытия явных запретов более узкими допущениями."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .manifests import ManifestError, ManifestPlan, ManifestStore

BLOCKING_STATUSES = frozenset({"prohibited", "permission_required"})
CHECKED_OPERATIONS = frozenset({"acquisition", "storage"})


def _timestamp(value: str) -> datetime:
    """Проверить временную метку до сравнения истории оснований."""

    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))

    except (ValueError, AttributeError) as exception:
        raise ManifestError("Для проверки ограничений нужна корректная временная метка") from exception

    if result.utcoffset() is None:
        raise ManifestError("Временная метка проверки ограничений должна содержать часовой пояс")

    return result


def _active_restrictions(
    store: ManifestStore,
    rights: dict[str, dict[str, Any]],
    *,
    at: datetime,
    known_at: datetime,
) -> list[dict[str, Any]]:
    """Найти действовавшие запреты с учётом поздней записи, замены и срока действия."""

    return [
        right for right in store._rights_active_at(rights, at, known_at=known_at)
        if right["status"] in BLOCKING_STATUSES and right["operation"] in CHECKED_OPERATIONS
        and (
            right.get("rights_expires_at") is None or
            date.fromisoformat(right["rights_expires_at"]) >= at.date()
        )
    ]


def _reject_restriction(right: dict[str, Any], subject_id: str, at: datetime) -> None:
    """Объяснить, какое существующее ограничение нельзя заменить исследовательским допущением."""

    raise ManifestError(
        f"Партия конфликтует с основанием {right['rights_record_id']!r}: "
        f"{right['operation']} имеет статус {right['status']} для {subject_id!r} "
        f"на {at.isoformat()}. DEC-012 не отменяет это ограничение."
    )


def ensure_no_conflicting_rights(
    store: ManifestStore,
    plan: ManifestPlan,
    registered_at: str,
) -> None:
    """Не разрешать сбор и хранение партии вопреки любому применимому явному ограничению."""

    recorded_at = _timestamp(registered_at)
    rights = {right["rights_record_id"]: right for right in store.records("rights")}
    works = {work["work_id"]: work for work in store.records("works")}
    artifacts = {artifact["artifact_record_id"]: artifact for artifact in store.records("artifacts")}
    works.update({work["work_id"]: work for work in plan.works})
    artifacts.update({artifact["artifact_record_id"]: artifact for artifact in plan.artifacts})
    active_by_time: dict[datetime, list[dict[str, Any]]] = {}

    def restrictions_at(at: datetime) -> list[dict[str, Any]]:
        """Повторно использовать результат временного отбора для совпадающих дат."""

        if at > recorded_at:
            raise ManifestError("Регистрация партии не может предшествовать получению исходников")

        if at not in active_by_time:
            active_by_time[at] = _active_restrictions(store, rights, at=at, known_at=recorded_at)

        return active_by_time[at]

    for event in plan.retrieval_events:
        work, artifact = store._event_context(event, works, artifacts)

        if event["request_context_type"] == "artifact" and (artifact is None or work is None):
            raise ManifestError("В плане партии не найден контекст артефакта события получения")

        if event["request_context_type"] == "work" and work is None:
            raise ManifestError("В плане партии не найдена работа события получения")

        retrieved_at = _timestamp(event["retrieved_at"])
        moments = ((retrieved_at, CHECKED_OPERATIONS), (recorded_at, frozenset({"storage"})))

        for at, operations in moments:
            for right in restrictions_at(at):
                if right["operation"] not in operations:
                    continue

                if not store._right_matches_operation_context(right, event, right["operation"]):
                    continue

                if store._right_applies_to_event(
                    right, event, work, artifact,
                    source_group_ids=store._event_source_group_ids(event),
                ):
                    _reject_restriction(right, event["retrieval_id"], at)

    # Проверяем также артефакты без нового события: повторный импорт может
    # использовать уже зарегистрированный ответ, сохраняя сам файл в плане.
    for artifact in plan.artifacts:
        work = works.get(artifact["work_id"])

        if work is None:
            raise ManifestError("В плане партии не найдена работа сохраняемого артефакта")

        moments = [(recorded_at, frozenset({"storage"}))]
        moments.extend((_timestamp(item["retrieved_at"]), CHECKED_OPERATIONS)
                       for item in artifact.get("retrievals", []))

        for at, operations in moments:
            for right in restrictions_at(at):
                if right["operation"] not in operations:
                    continue

                if (
                    store._right_matches_operation_context(right, artifact, right["operation"]) and
                    store._rights_apply(right, work, artifact)
                ):
                    _reject_restriction(right, artifact["artifact_record_id"], at)
