"""Узкие основания DEC-012 для уже проверенных ответов одной партии журналов."""

from __future__ import annotations

import copy
import re

from datetime import datetime
from typing import Any

from src.collect.base import HttpResponseSnapshot

from .manifests import ManifestPlan, PlannedBlob, canonical_json, sha256_bytes

SOURCE_GROUP_IDS = {
    "S02_JETP_LETTERS",
    "S04_JETP",
    "S05_IOFFE",
    "S06_QUANTUM_ELECTRONICS",
}
COMMITMENTS = (
    "closed_noncommercial_research",
    "no_redistribution",
    "no_access_controls_bypassed",
    "no_explicit_applicable_prohibition_found",
    "robots_checked",
)
RESEARCH_CONDITION = (
    "Применяется ранее принятое проектное допущение DEC-012 только к указанному "
    "проверенному ответу: закрытое некоммерческое исследование, без публикации "
    "или передачи исходных материалов третьим лицам и без обхода ограничений доступа."
)
BATCH_CONDITION = (
    "Сохранены исходный ответ и его происхождение; получение не нарушало "
    "зафиксированные ограничения доступа и robots.txt. Эти обстоятельства "
    "проверяются при регистрации по сохранённым свидетельствам. "
    "Новые запросы, обучение, оценка и публикация модели этим основанием не разрешаются."
)


def batch_response_rights_ids(
    batch_id: str,
    response: HttpResponseSnapshot,
) -> tuple[str, str]:
    """Определить ID оснований до построения события, не создавая циклический хеш."""

    if not isinstance(response, HttpResponseSnapshot):
        raise ValueError("Нужен исходный HttpResponseSnapshot проверенного ответа")

    return _response_rights_ids(
        batch_id,
        response.metadata_sha256(),
        sha256_bytes(response.body),
    )


def attach_batch_rights(
    plan: ManifestPlan,
    *,
    batch_id: str,
    registered_at: str,
    evidence: dict[str, Any],
) -> ManifestPlan:
    """
    Добавить основания сбора и хранения только для точных ответов партии.

    Исходный план не меняется. ``evidence`` содержит снимок принятой политики,
    проверенные пары SHA-256 ответа и его метаданных, а также фактическую оценку
    условий. Дата записи всегда текущая для импорта; ``effective_from`` описывает
    действие ранее принятого проектного допущения, а не разрешение издателя.
    """

    _validate_batch_id(batch_id)
    registered_timestamp = _timestamp(registered_at, "registered_at")

    if plan.rights or plan.condition_fulfilments or plan.operation_decisions:
        raise ValueError("Основания партии добавляются до прав и решений о допустимости")

    if plan.identity_conflicts:
        raise ValueError("Сначала необходимо разрешить конфликты идентичности партии")

    if not plan.retrieval_events:
        raise ValueError("Партия не содержит событий получения")

    policy_timestamp, reviewed_responses = _validate_evidence(evidence)
    artifacts = {artifact["artifact_record_id"]: artifact for artifact in plan.artifacts}
    works = {work["work_id"]: work for work in plan.works}
    scopes = []

    for event in plan.retrieval_events:
        scope = _validate_event(
            event,
            artifacts=artifacts,
            works=works,
            batch_id=batch_id,
            registered_timestamp=registered_timestamp,
            policy_timestamp=policy_timestamp,
            reviewed_responses=reviewed_responses,
        )
        scopes.append(scope)

    evidence_record = {
        "schema_version": "journal-batch-rights-evidence-v1",
        "batch_id": batch_id,
        "registered_at": registered_at,
        "assessment": copy.deepcopy(evidence),
        "interpretation": (
            "Текущая регистрация ранее полученных ответов по внутреннему допущению "
            "DEC-012. Не является разрешением издателя или юридической гарантией. "
            "Выполнение условий оценивается ретроспективно по сохранённым свидетельствам; "
            "дата получения ответа и дата внесения сведений разделены."
        ),
        "responses": [
            {
                "retrieval_id": event["retrieval_id"],
                "retrieved_at": event["retrieved_at"],
                "requested_url": event["requested_url"],
                "final_url": event["final_url"],
                "response_metadata_sha256": event["response_metadata_sha256"],
                "response_sha256": event["response_sha256"],
                "scope_type": scope[0],
                "scope_id": scope[1],
            }
            for event, scope in zip(plan.retrieval_events, scopes, strict=True)
        ],
    }
    evidence_bytes = (canonical_json(evidence_record) + "\n").encode("utf-8")
    evidence_sha256 = sha256_bytes(evidence_bytes)
    result = copy.deepcopy(plan)
    result.blobs.append(
        PlannedBlob(
            relative_path=f"data/raw/journal_registration/evidence/{evidence_sha256}.json",
            data=evidence_bytes,
            sha256=evidence_sha256,
        )
    )

    for event, scope in zip(result.retrieval_events, scopes, strict=True):
        for operation, rights_record_id in zip(
            ("acquisition", "storage"), event["rights_record_ids"], strict=True,
        ):
            right = _build_right(
                event,
                operation=operation,
                rights_record_id=rights_record_id,
                scope=scope,
                registered_at=registered_at,
                evidence_sha256=evidence_sha256,
            )
            result.rights.append(right)
            result.condition_fulfilments.extend(
                _build_fulfilment(right, condition)
                for condition in right["rights_conditions"]
            )

    return result


def _validate_batch_id(batch_id: str) -> None:
    """Не принимать пустое или нестроковое имя фиксированной партии."""

    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id должен быть непустой строкой")


def _timestamp(value: Any, field_name: str) -> datetime:
    """Проверить временную метку вместе с явно заданным часовым поясом."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name}: нужна строковая дата ISO 8601")

    try:
        timestamp = datetime.fromisoformat(value)

    except ValueError as exception:
        raise ValueError(f"{field_name}: неверная дата ISO 8601") from exception

    if timestamp.utcoffset() is None:
        raise ValueError(f"{field_name}: необходимо указать часовой пояс")

    return timestamp


def _digest(value: Any, field_name: str) -> str:
    """Проверить контрольную сумму перед использованием в свидетельстве."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name}: требуется SHA-256 из 64 строчных цифр и букв")

    return value


def _response_rights_ids(
    batch_id: str,
    metadata_sha256: str,
    response_sha256: str,
) -> tuple[str, str]:
    """Привязать два основания к партии и точным байтам одного ответа."""

    _validate_batch_id(batch_id)
    identity = {
        "batch_id": batch_id,
        "response_metadata_sha256": _digest(metadata_sha256, "response_metadata_sha256"),
        "response_sha256": _digest(response_sha256, "response_sha256"),
    }
    digest = sha256_bytes(canonical_json(identity).encode("utf-8"))

    return (
        f"right:journal-batch:{digest}:acquisition",
        f"right:journal-batch:{digest}:storage",
    )


def _validate_evidence(evidence: dict[str, Any]) -> tuple[datetime, set[tuple[str, str]]]:
    """Проверить явные подтверждения и точный перечень рассмотренных ответов."""

    if not isinstance(evidence, dict):
        raise ValueError("Свидетельство проверки партии должно быть JSON-объектом")

    policy = evidence.get("policy")

    if not isinstance(policy, dict) or policy.get("decision_id") != "DEC-012":
        raise ValueError("В свидетельстве нужен снимок политики DEC-012")

    if not isinstance(policy.get("snapshot"), str) or not policy["snapshot"].strip():
        raise ValueError("В свидетельстве отсутствует текст принятой политики")

    policy_timestamp = _timestamp(policy.get("adopted_at"), "policy.adopted_at")
    _digest(evidence.get("prepared_batch_sha256"), "prepared_batch_sha256")
    commitments = evidence.get("commitments")

    if not isinstance(commitments, dict) or any(
        commitments.get(name) is not True for name in COMMITMENTS
    ):
        raise ValueError("Не подтверждены все условия закрытого сбора и хранения партии")

    responses = evidence.get("reviewed_responses")

    if not isinstance(responses, list) or not responses:
        raise ValueError("Нет точного перечня проверенных ответов reviewed_responses")

    reviewed_responses = set()

    for response in responses:
        if not isinstance(response, dict):
            raise ValueError("Проверенный ответ должен быть JSON-объектом")

        reviewed_responses.add(
            (
                _digest(response.get("response_metadata_sha256"), "response_metadata_sha256"),
                _digest(response.get("response_sha256"), "response_sha256"),
            )
        )

    return policy_timestamp, reviewed_responses


def _validate_event(
    event: dict[str, Any],
    *,
    artifacts: dict[str, dict[str, Any]],
    works: dict[str, dict[str, Any]],
    batch_id: str,
    registered_timestamp: datetime,
    policy_timestamp: datetime,
    reviewed_responses: set[tuple[str, str]],
) -> tuple[str, str]:
    """Ограничить основание реально проверенным ответом и точной областью действия."""

    retrieved_timestamp = _timestamp(event["retrieved_at"], "retrieved_at")

    if (
        _timestamp(event["created_at"], "created_at") != registered_timestamp
        or registered_timestamp < retrieved_timestamp
    ):
        raise ValueError("Время регистрации события должно совпадать с registered_at и не предшествовать скачиванию")

    if policy_timestamp > retrieved_timestamp:
        raise ValueError("Нельзя задним числом применять политику, принятую после скачивания")

    response_identity = (event["response_metadata_sha256"], event["response_sha256"])

    if response_identity not in reviewed_responses:
        raise ValueError("Событие отсутствует в точном перечне проверенных ответов")

    expected_ids = _response_rights_ids(batch_id, *response_identity)

    if list(event["rights_record_ids"]) != list(expected_ids):
        raise ValueError("Событие должно ссылаться только на два заранее рассчитанных основания партии")

    if event["outcome"] != "succeeded" or event["http_status"] != 200:
        raise ValueError("Основание партии применимо только к успешно полученному HTTP 200")

    if event["acquisition_method"] != "crawler" or event["acquisition_scope"] != "bulk":
        raise ValueError("Партия относится только к уже выполненному сборщиком массовому сбору")

    context = event["request_context_type"]

    if context == "artifact":
        artifact = artifacts.get(event["request_context_id"])

        if artifact is None or artifact.get("sha256") != event["response_sha256"]:
            raise ValueError("Контекст события должен указывать на точный исходный артефакт")

        work = works.get(artifact["work_id"])

        if work is None or work["source_group_id"] not in SOURCE_GROUP_IDS:
            raise ValueError("Источник не входит в проверяемую партию журналов DEC-012")

        if event["source_group_id"] not in {None, work["source_group_id"]}:
            raise ValueError("Источник события не совпадает с источником работы")

        if work["source_group_id"] == "S06_QUANTUM_ELECTRONICS" and (
            artifact["representation"] != "html" or artifact["content_role"] != "metadata_only"
        ):
            raise ValueError("Для Квантовой электроники проверены только HTML-метаданные, не PDF")

        return "artifact", artifact["artifact_record_id"]

    if context == "source":
        if event["source_group_id"] not in SOURCE_GROUP_IDS:
            raise ValueError("Источник не входит в проверяемую партию журналов DEC-012")

        content_type = str(event["response_headers"].get("content-type", "")).lower()

        if not content_type.startswith("text/html"):
            raise ValueError("Общий ответ источника должен быть HTML-оглавлением")

        return "retrieval", event["retrieval_id"]

    raise ValueError("Нужно узкое событие в контексте artifact или общее оглавление source")


def _build_right(
    event: dict[str, Any],
    *,
    operation: str,
    rights_record_id: str,
    scope: tuple[str, str],
    registered_at: str,
    evidence_sha256: str,
) -> dict[str, Any]:
    """Зафиксировать внутреннее условное основание, не изображая согласие издателя."""

    return {
        "schema_version": "rights-v1",
        "created_at": registered_at,
        "rights_record_id": rights_record_id,
        "scope_type": scope[0],
        "scope_id": scope[1],
        "operation": operation,
        "status": "conditional",
        "access_basis": (
            "Внутреннее проектное допущение DEC-012 для точного ранее полученного "
            "ответа проверенной партии. Не является разрешением правообладателя; "
            "не распространяется на обучение, оценку, новые загрузки или публикацию."
        ),
        "basis_type": "lawful_access_research_assumption",
        "acquisition_method": "crawler" if operation == "acquisition" else None,
        "acquisition_scope": "bulk" if operation == "acquisition" else None,
        "terms_url": None,
        "rights_checked_at": datetime.fromisoformat(registered_at).date().isoformat(),
        "derivative_scope": None,
        "rights_conditions": [RESEARCH_CONDITION, BATCH_CONDITION],
        "conditions_satisfied_at": None,
        "conditions_evidence_sha256": None,
        "rights_evidence_sha256": evidence_sha256,
        "rights_expires_at": None,
        "supersedes_rights_record_id": None,
        "effective_from": event["retrieved_at"],
        "effective_from_evidence_sha256": evidence_sha256,
    }


def _build_fulfilment(right: dict[str, Any], condition: str) -> dict[str, Any]:
    """Отдельно записать дату оценки и подтверждённое время выполнения условия."""

    identity = {
        "rights_record_id": right["rights_record_id"],
        "condition": condition,
        "scope_type": right["scope_type"],
        "scope_id": right["scope_id"],
    }
    digest = sha256_bytes(canonical_json(identity).encode("utf-8"))

    return {
        "schema_version": "condition-fulfilments-v1",
        "fulfilment_id": f"fulfilment:journal-batch:{digest}",
        "created_at": right["created_at"],
        "rights_record_id": right["rights_record_id"],
        "condition": condition,
        "subject_type": right["scope_type"],
        "subject_id": right["scope_id"],
        "status": "satisfied",
        "satisfied_at": right["effective_from"],
        "expires_at": None,
        "evidence_sha256": right["rights_evidence_sha256"],
        "supersedes_fulfilment_id": None,
    }
