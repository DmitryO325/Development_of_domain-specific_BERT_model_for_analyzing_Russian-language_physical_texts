"""Точечные основания хранения TXT из явно согласованной партии PDF."""

from __future__ import annotations

import copy
import json
import re

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .manifests import (
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
    canonical_json,
    sha256_bytes,
)

SCOPE_LIMITS = {
    "new_downloads",
    "include_recorded_pdf",
    "include_previously_processed_pdf",
    "include_later_downloads",
    "change_genre_or_labels",
    "model_evaluation",
    "ml_training",
    "redistribution",
    "dataset_release",
    "checkpoint_release",
    "work_eligibility_promotion",
    "qa_promotion",
    "source_or_work_wide_storage_rights",
    "retroactive_acquisition_rights_for_text",
}
STORAGE_CONDITIONS = (
    "Зафиксировано обязательство проекта о закрытом некоммерческом хранении "
    "без публикации и передачи текстов; разрешение правообладателя не утверждается.",
    "Проверены точные байты PDF согласованной партии, происхождение TXT, версия "
    "извлечения и контрольные суммы; применимые ограничения не отменяются.",
)


@dataclass(frozen=True)
class DerivedTextApproval:
    """Проверенный локальный пакет согласия с неизменяемыми исходными файлами."""

    project_root: Path
    approval_path: Path
    approval_sha256: str
    approval: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    review: dict[str, Any]
    review_sha256: str
    _records_cache: dict[tuple[Path, str], tuple[str, list[dict[str, Any]]]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def _records(self, store: ManifestStore, kind: str) -> list[dict[str, Any]]:
        """Повторно проверять схему только при изменении действительных байтов реестра."""

        path = store.path_for(kind)
        digest = sha256_bytes(path.read_bytes() if path.exists() else b"")
        key = (store.manifest_dir.resolve(), kind)
        previous = self._records_cache.get(key)

        if previous is None or previous[0] != digest:
            self._records_cache[key] = (digest, store.records(kind))

        return self._records_cache[key][1]

    def validate_parent(
        self,
        parent: dict[str, Any],
        *,
        store: ManifestStore,
        at: str,
    ) -> None:
        """До OCR сверить состав партии, PDF и действующие ограничения реестра."""

        timestamp = _timestamp(at)

        if timestamp < _timestamp(self.approval["recorded_at"]):
            raise ValueError("Извлечение не может предшествовать согласованию партии")

        _verified_file(
            self.project_root,
            self.approval_path.relative_to(self.project_root).as_posix(),
            self.approval_sha256,
        )
        _verified_file(
            self.project_root,
            self.approval["candidate_file"],
            self.approval["candidate_sha256"],
        )

        candidate = _candidate_for_parent(self, parent)
        stored = {
            artifact["artifact_record_id"]: artifact
            for artifact in self._records(store, "artifacts")
        }

        if stored.get(parent["artifact_record_id"]) != parent:
            raise ValueError("Родительский PDF не совпадает с текущей записью реестра")

        if (
            parent.get("representation") != "pdf"
            or parent.get("acquisition_status") != "retrieved"
            or parent.get("parent_artifact_id") is not None
        ):
            raise ValueError(
                "В партии разрешён только исходный PDF retrieved, не recorded"
            )

        pdf_bytes = _local_path(
            self.project_root, candidate["pdf_path"], prefix="data"
        ).read_bytes()

        if (
            sha256_bytes(pdf_bytes) != candidate["pdf_sha256"]
            or len(pdf_bytes) != candidate["pdf_bytes"]
        ):
            raise ValueError("Байты PDF изменились после согласования партии")

        work = _work_for_parent(self, store, parent)

        if work["eligibility_status"] != "pending" or work.get("duplicate_of_work_id"):
            raise ValueError(
                "Работа партии должна оставаться pending без отметки дубликата"
            )

        if (
            work["source_id"] != candidate["source_id"]
            or work["genre"] != candidate["genre"]
        ):
            raise ValueError(
                "Источник или жанр работы изменился после подготовки партии"
            )

        rights, fulfilments = _rights_state(self, store, timestamp)
        applicable = [
            right for right in rights if store._rights_apply(right, work, parent)
        ]
        _require_storage_conditions(
            store, applicable, fulfilments, work, parent, timestamp, required=True
        )
        evidence_digests = {item["sha256"] for item in self.review["parent_evidence"]}

        for rights_record_id in candidate["parent_rights_record_ids"]:
            matches = [
                right
                for right in rights
                if right["rights_record_id"] == rights_record_id
            ]

            if (
                not matches
                or matches[0]["rights_evidence_sha256"] not in evidence_digests
            ):
                raise ValueError(
                    "Основание исходного PDF заменено либо не подтверждено проверенным свидетельством"
                )

        decisions = self._records(store, "operation_decisions")

        if not any(
            decision["subject_type"] == "artifact"
            and decision["subject_id"] == parent["artifact_record_id"]
            and decision["operation"] == "acquisition"
            and decision["status"] == "allowed"
            and _timestamp(decision["created_at"]) <= timestamp
            for decision in decisions
        ):
            raise ValueError(
                "Не найдено разрешающее историческое решение о получении исходного PDF"
            )

        for child in stored.values():
            if child.get("parent_artifact_id") != parent["artifact_id"]:
                continue

            own_right_id = _storage_right_id(self, child)

            if own_right_id not in child["rights_record_ids"]:
                raise ValueError(
                    "PDF уже обработан вне данного согласования; повторное включение запрещено"
                )


def load_derived_text_approval(
    path: Path, *, project_root: Path
) -> DerivedTextApproval:
    """Прочитать согласие и проверить привязку к списку, сводке и свидетельствам."""

    root = project_root.resolve()
    approval_path = path.resolve() if path.is_absolute() else (root / path).resolve()

    if not approval_path.is_relative_to(root):
        raise ValueError("Согласие должно находиться внутри проекта")

    approval_bytes = approval_path.read_bytes()
    approval = _json_object(approval_bytes, "согласие")
    expected = {
        "format_version": "journal-derived-text-scope-approval-v1",
        "decision_id": "DEC-024",
        "policy_id": "DEC-012",
        "approved": True,
        "approved_by": "user",
        "basis_type": "lawful_access_research_assumption",
        "scope": "closed_noncommercial_local_extraction_and_derived_text_storage",
    }

    if (
        any(approval.get(key) != value for key, value in expected.items())
        or approval.get("approved") is not True
    ):
        raise ValueError("Не подтверждено точное проектное согласие DEC-024/DEC-012")

    _timestamp(approval.get("recorded_at"))
    limits = approval.get("scope_limits")

    if (
        not isinstance(limits, dict)
        or set(limits) != SCOPE_LIMITS
        or any(value is not False for value in limits.values())
    ):
        raise ValueError("Границы согласия изменены: дополнительные операции запрещены")

    if approval.get("candidate_identity_fields") != [
        "work_id",
        "pdf_artifact_record_id",
        "pdf_sha256",
    ]:
        raise ValueError("Согласие должно фиксировать работу, запись PDF и его SHA-256")

    if not isinstance(approval.get("conditions"), list) or not approval["conditions"]:
        raise ValueError("В согласии отсутствуют условия закрытого использования")

    candidate_bytes = _verified_file(
        root, approval.get("candidate_file"), approval.get("candidate_sha256")
    )
    candidates = tuple(
        _json_object(line, "кандидат")
        for line in candidate_bytes.splitlines()
        if line.strip()
    )

    if (
        type(approval.get("candidate_count")) is not int
        or not candidates
        or len(candidates) != approval["candidate_count"]
    ):
        raise ValueError("Количество PDF не соответствует согласованию")

    seen: set[str] = set()

    for candidate in candidates:
        _validate_candidate(candidate)

        if candidate["pdf_sha256"] in seen:
            raise ValueError("В согласованном списке повторяется PDF")

        seen.add(candidate["pdf_sha256"])

    summary = _json_object(
        _verified_file(
            root,
            approval.get("preparation_summary"),
            approval.get("preparation_summary_sha256"),
        ),
        "сводка подготовки",
    )

    summary_counts = summary.get("counts")

    if (
        summary.get("candidate_sha256") != approval["candidate_sha256"]
        or not isinstance(summary_counts, dict)
        or summary_counts.get("candidates") != len(candidates)
    ):
        raise ValueError("Сводка подготовки относится к другой партии")

    approval_sha256 = sha256_bytes(approval_bytes)
    review_path = _local_path(
        root,
        approval_path.with_name("conditions_review.json").relative_to(root).as_posix(),
    )
    review_bytes = review_path.read_bytes()
    review = _json_object(review_bytes, "проверка условий")

    if (
        review.get("format_version") != "journal-derived-text-conditions-review-v1"
        or review.get("decision_id") != "DEC-024"
        or review.get("scope") != "local_saved_evidence_review_only"
        or review.get("approval_sha256") != approval_sha256
        or review.get("candidate_sha256") != approval["candidate_sha256"]
        or review.get("parent_pdf_count") != len(candidates)
    ):
        raise ValueError(
            "Проверка условий не привязана к этому согласию и точному составу партии"
        )

    if (
        not isinstance(review.get("parent_evidence"), list)
        or not review["parent_evidence"]
    ):
        raise ValueError("Нет проверенных свидетельств исходных PDF")

    for evidence in review["parent_evidence"]:
        if not isinstance(evidence, dict):
            raise ValueError("Свидетельство исходного PDF должно быть JSON-объектом")

        payload = _verified_file(root, evidence.get("path"), evidence.get("sha256"))

        if type(evidence.get("bytes")) is not int or len(payload) != evidence["bytes"]:
            raise ValueError("Размер свидетельства исходного PDF изменился")

    return DerivedTextApproval(
        root,
        approval_path,
        approval_sha256,
        approval,
        candidates,
        review,
        sha256_bytes(review_bytes),
    )


def attach_derived_storage(
    plan: ManifestPlan,
    *,
    scope: DerivedTextApproval,
    parent: dict[str, Any],
    store: ManifestStore,
    at: str,
) -> ManifestPlan:
    """Добавить только условное хранение конкретного TXT без новых получений."""

    if (
        len(plan.artifacts) != 1
        or plan.rights
        or plan.condition_fulfilments
        or plan.works
        or plan.retrieval_events
        or plan.operation_decisions
        or plan.work_aliases
        or plan.identity_conflicts
        or plan.work_update_reasons
    ):
        raise ValueError(
            "Нужен отдельный план одного TXT без прав и посторонних реестровых изменений"
        )

    scope.validate_parent(parent, store=store, at=at)
    result = copy.deepcopy(plan)
    child = result.artifacts[0]
    _validate_child(child, parent, result.blobs, scope.project_root)
    work = _work_for_parent(scope, store, parent)
    timestamp = _timestamp(at)
    rights, fulfilments = _rights_state(scope, store, timestamp)
    applicable = [right for right in rights if store._rights_apply(right, work, child)]
    _require_storage_conditions(
        store, applicable, fulfilments, work, child, timestamp, required=False
    )
    right_id = _storage_right_id(scope, child)
    previous = [
        right
        for right in scope._records(store, "rights")
        if right["rights_record_id"] == right_id
    ]

    if previous:
        right = previous[0]
        _validate_existing_right(right, child, scope)
        child["rights_record_ids"] = [right_id]

        return result

    if _timestamp(child["created_at"]) != timestamp:
        raise ValueError(
            "Новое основание хранения создаётся одновременно с новым TXT, не задним числом"
        )

    evidence = {
        "schema_version": "derived-text-storage-evidence-v1",
        "recorded_at": at,
        "decision_id": "DEC-024",
        "policy_id": "DEC-012",
        "approval_sha256": scope.approval_sha256,
        "approval_snapshot": scope.approval,
        "conditions_review_sha256": scope.review_sha256,
        "candidate_sha256": scope.approval["candidate_sha256"],
        "candidate": _candidate_for_parent(scope, parent),
        "parent_artifact": parent,
        "child_artifact_record_id": child["artifact_record_id"],
        "child_sha256": child["sha256"],
        "child_bytes": child["bytes"],
        "child_path": child["path"],
        "extraction_version": child["extraction_version"],
        "extraction_method": child["extraction_method"],
        "checked": [
            "Точные байты PDF и TXT",
            "Происхождение и версия извлечения",
            "Актуальные локальные основания и условия хранения",
        ],
        "limitations": [
            "Закрытое использование — обязательство проекта; ACL и облачные копии не проверялись.",
            "Проверены локальные записи; новая проверка условий сайтов и юридическая оценка не выполнялись.",
            "Разрешение правообладателя, оценка моделей, обучение и публикация не утверждаются.",
        ],
    }
    evidence_bytes = (canonical_json(evidence) + "\n").encode("utf-8")
    digest = sha256_bytes(evidence_bytes)
    right = _build_storage_right(right_id, child, at, digest)
    result.rights.append(right)
    result.condition_fulfilments.extend(
        _build_fulfilment(right, condition) for condition in STORAGE_CONDITIONS
    )
    result.blobs.append(
        PlannedBlob(
            f"data/raw/derived_text_registration/evidence/{digest}.json",
            evidence_bytes,
            digest,
        )
    )
    child["rights_record_ids"] = [right_id]

    return result


def _timestamp(value: Any) -> datetime:
    """Проверить дату с явно указанным часовым поясом."""

    if not isinstance(value, str):
        raise ValueError("Нужна строковая дата ISO 8601")

    try:
        timestamp = datetime.fromisoformat(value)

    except ValueError as exception:
        raise ValueError("Неверная дата ISO 8601") from exception

    if timestamp.utcoffset() is None:
        raise ValueError("В дате нужен часовой пояс")

    return timestamp


def _json_object(payload: bytes, description: str) -> dict[str, Any]:
    """Разобрать JSON-объект с понятной ошибкой для локального свидетельства."""

    try:
        record = json.loads(payload, object_pairs_hook=_unique_json_pairs)

    except (ValueError, UnicodeError) as exception:
        raise ValueError(f"Некорректный JSON: {description}") from exception

    if not isinstance(record, dict):
        raise ValueError(f"Нужен JSON-объект: {description}")

    return record


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Не допускать неоднозначного значения условий при повторе JSON-ключа."""

    record: dict[str, Any] = {}

    for name, value in pairs:
        if name in record:
            raise ValueError(f"Повторный JSON-ключ: {name}")

        record[name] = value

    return record


def _local_path(root: Path, value: Any, *, prefix: str | None = None) -> Path:
    """Разрешить относительный путь без выхода и символьных ссылок за проект."""

    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ValueError("Нужен безопасный относительный путь внутри проекта")

    path = (root / value).resolve()
    allowed_root = root / prefix if prefix else root

    if not path.is_relative_to(allowed_root):
        raise ValueError("Путь выходит за разрешённый каталог проекта")

    return path


def _verified_file(root: Path, value: Any, digest: Any) -> bytes:
    """Прочитать локальный файл только при совпадении контрольной суммы."""

    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Нужна корректная контрольная сумма SHA-256")

    payload = _local_path(root, value).read_bytes()

    if sha256_bytes(payload) != digest:
        raise ValueError(f"SHA-256 файла {value!r} не совпадает с согласованием")

    return payload


def _validate_candidate(candidate: dict[str, Any]) -> None:
    """Отклонить неполные карточки, recorded и расширение согласованного состава."""

    required = (
        "work_id",
        "pdf_artifact_record_id",
        "pdf_artifact_id",
        "pdf_path",
        "source_id",
        "genre",
        "title",
    )

    if any(
        not isinstance(candidate.get(name), str) or not candidate[name]
        for name in required
    ):
        raise ValueError("В карточке партии отсутствует точная идентичность PDF")

    digest = candidate.get("pdf_sha256")

    if (
        candidate.get("format_version") != "journal-extraction-candidate-v1"
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or candidate["pdf_artifact_id"] != f"sha256:{digest}"
        or candidate["pdf_artifact_record_id"] != f"artifact-record:sha256:{digest}"
        or type(candidate.get("pdf_bytes")) is not int
        or candidate["pdf_bytes"] <= 0
        or candidate.get("work_eligibility_status") != "pending"
        or not isinstance(candidate.get("parent_rights_record_ids"), list)
        or not candidate["parent_rights_record_ids"]
        or any(
            not isinstance(right_id, str) or not right_id.strip()
            for right_id in candidate["parent_rights_record_ids"]
        )
    ):
        raise ValueError("Некорректная карточка точной партии PDF")


def _candidate_for_parent(
    scope: DerivedTextApproval, parent: dict[str, Any]
) -> dict[str, Any]:
    """Найти PDF только по согласованной записи, байтам и работе одновременно."""

    for candidate in scope.candidates:
        if candidate["pdf_artifact_record_id"] != parent.get("artifact_record_id"):
            continue

        pairs = {
            "work_id": "work_id",
            "pdf_artifact_id": "artifact_id",
            "pdf_sha256": "sha256",
            "pdf_path": "path",
            "pdf_bytes": "bytes",
            "parent_rights_record_ids": "rights_record_ids",
        }

        if any(candidate[name] != parent.get(field) for name, field in pairs.items()):
            raise ValueError("Родительский PDF не совпадает с согласованной карточкой")

        return candidate

    raise ValueError("PDF не входит в согласованную партию")


def _work_for_parent(
    scope: DerivedTextApproval,
    store: ManifestStore,
    parent: dict[str, Any],
) -> dict[str, Any]:
    """Найти зарегистрированную работу без создания новых библиографических данных."""

    matches = [
        work
        for work in scope._records(store, "works")
        if work["work_id"] == parent["work_id"]
    ]

    if len(matches) != 1:
        raise ValueError("Не найдена единственная работа исходного PDF")

    return matches[0]


def _rights_state(
    scope: DerivedTextApproval,
    store: ManifestStore,
    at: datetime,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Применить существующие правила активности прав и истории условий."""

    rights = {
        right["rights_record_id"]: right for right in scope._records(store, "rights")
    }
    fulfilments = {
        item["fulfilment_id"]: item
        for item in scope._records(store, "condition_fulfilments")
    }

    return store._rights_active_at(rights, at), fulfilments


def _require_storage_conditions(
    store: ManifestStore,
    rights: list[dict[str, Any]],
    fulfilments: dict[str, dict[str, Any]],
    work: dict[str, Any],
    artifact: dict[str, Any],
    at: datetime,
    *,
    required: bool,
) -> None:
    """Не разрешать узкому новому основанию отменять другие условия хранения."""

    storage = [right for right in rights if right["operation"] == "storage"]

    if required and not storage:
        raise ValueError("Для исходного PDF отсутствует действующее основание хранения")

    if any(
        not store._right_permits_at(right, fulfilments, work, artifact, at=at)
        for right in storage
    ):
        raise ValueError(
            "Применимое ограничение или невыполненное условие хранения запрещает обработку"
        )


def _validate_child(
    child: dict[str, Any], parent: dict[str, Any], blobs: list[PlannedBlob], root: Path
) -> None:
    """Сверить действительный TXT с происхождением и не допустить посторонних файлов."""

    if (
        child.get("work_id") != parent["work_id"]
        or child.get("parent_artifact_id") != parent["artifact_id"]
        or child.get("representation") not in {"plain_text", "ocr_text"}
        or child.get("extraction_method") not in {"pdf", "pdf_ocr_layout"}
        or child.get("qa_status") != "not_evaluated"
        or child.get("extraction_status") != "succeeded"
        or child.get("retrievals") != parent["retrievals"]
    ):
        raise ValueError(
            "План не соответствует производному непроверенному TXT данного PDF"
        )

    path = _local_path(root, child.get("path"), prefix="data/extracted")

    if path.suffix != ".txt" or len(blobs) != 1:
        raise ValueError(
            "Основание относится только к одному полному TXT в data/extracted"
        )

    blob = blobs[0]
    digest = sha256_bytes(blob.data)

    if (
        blob.relative_path != child["path"]
        or digest != blob.sha256
        or digest != child.get("sha256")
        or child.get("bytes") != len(blob.data)
        or child.get("artifact_id") != f"sha256:{digest}"
        or child.get("artifact_record_id") != f"artifact-record:sha256:{digest}"
    ):
        raise ValueError(
            "Байты производного TXT не соответствуют его реестровой записи"
        )

    try:
        text = blob.data.decode("utf-8")

    except UnicodeError as exception:
        raise ValueError(
            "TXT должен содержать UTF-8, а не другой формат файла"
        ) from exception

    if not text.strip() or not child.get("extraction_version"):
        raise ValueError("Для TXT нужны непустой текст и версия извлечения")


def _storage_right_id(scope: DerivedTextApproval, child: dict[str, Any]) -> str:
    """Привязать основание к согласию и точному идентификатору текста."""

    identity = {
        "approval_sha256": scope.approval_sha256,
        "artifact_record_id": child["artifact_record_id"],
    }

    return f"right:derived-text:{sha256_bytes(canonical_json(identity).encode('utf-8'))}:storage"


def _build_storage_right(
    right_id: str, child: dict[str, Any], at: str, digest: str
) -> dict[str, Any]:
    """Создать условное хранение только одного уже вычисленного TXT."""

    return {
        "schema_version": "rights-v1",
        "created_at": at,
        "rights_record_id": right_id,
        "scope_type": "artifact",
        "scope_id": child["artifact_record_id"],
        "operation": "storage",
        "status": "conditional",
        "access_basis": "DEC-012 и DEC-024: проектное допущение только для закрытого хранения конкретного производного TXT; не разрешение издателя и не допуск к обучению.",
        "basis_type": "lawful_access_research_assumption",
        "acquisition_method": None,
        "acquisition_scope": None,
        "terms_url": None,
        "rights_checked_at": _timestamp(at).date().isoformat(),
        "derivative_scope": None,
        "rights_conditions": list(STORAGE_CONDITIONS),
        "conditions_satisfied_at": None,
        "conditions_evidence_sha256": None,
        "rights_evidence_sha256": digest,
        "rights_expires_at": None,
        "supersedes_rights_record_id": None,
    }


def _build_fulfilment(right: dict[str, Any], condition: str) -> dict[str, Any]:
    """Зафиксировать выполненную текущую проверку, не приписывая ей дату PDF."""

    identity = {"rights_record_id": right["rights_record_id"], "condition": condition}
    digest = sha256_bytes(canonical_json(identity).encode("utf-8"))

    return {
        "schema_version": "condition-fulfilments-v1",
        "fulfilment_id": f"fulfilment:derived-text:{digest}",
        "created_at": right["created_at"],
        "rights_record_id": right["rights_record_id"],
        "condition": condition,
        "subject_type": "artifact",
        "subject_id": right["scope_id"],
        "status": "satisfied",
        "satisfied_at": right["created_at"],
        "expires_at": None,
        "evidence_sha256": right["rights_evidence_sha256"],
        "supersedes_fulfilment_id": None,
    }


def _validate_existing_right(
    right: dict[str, Any], child: dict[str, Any], scope: DerivedTextApproval
) -> None:
    """При повторе сохранить исходную дату и убедиться в том же точечном согласии."""

    digest = right["rights_evidence_sha256"]
    evidence = _json_object(
        _verified_file(
            scope.project_root,
            f"data/raw/derived_text_registration/evidence/{digest}.json",
            digest,
        ),
        "свидетельство хранения TXT",
    )
    expected = _build_storage_right(
        right["rights_record_id"], child, right["created_at"], digest
    )

    if (
        right != expected
        or evidence.get("approval_sha256") != scope.approval_sha256
        or evidence.get("child_artifact_record_id") != child["artifact_record_id"]
    ):
        raise ValueError(
            "Существующее основание TXT не соответствует тому же согласованию"
        )
