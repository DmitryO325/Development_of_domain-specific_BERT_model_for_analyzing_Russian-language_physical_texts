"""Безопасная пакетная запись и семантический аудит реестров корпуса."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .schema_validation import SchemaCatalog, SchemaValidationError

REGISTRY_FILES = {
    "rights": "rights.jsonl",
    "works": "works.jsonl",
    "artifacts": "artifacts.jsonl",
}
PRIMARY_FIELDS = {
    "rights": "rights_record_id",
    "works": "work_id",
    "artifacts": "artifact_record_id",
}
SCOPE_SPECIFICITY = {
    "source_group": 1,
    "journal": 2,
    "source": 3,
    "work": 4,
    "artifact": 5,
}


class ManifestError(RuntimeError):
    """Базовая ошибка машинного реестра."""


class ManifestConflictError(ManifestError):
    """Один идентификатор встретился с разным содержимым."""


class _DuplicateJsonKeyError(ValueError):
    pass


@dataclass(frozen=True)
class PlannedBlob:
    """Неизменяемые байты, которые нужно положить в data/."""

    relative_path: str
    data: bytes
    sha256: str


@dataclass
class ManifestPlan:
    rights: list[dict[str, Any]] = field(default_factory=list)
    works: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    blobs: list[PlannedBlob] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommitResult:
    inserted: dict[str, int]
    unchanged: dict[str, int]
    written_blobs: int
    unchanged_blobs: int
    dry_run: bool


@dataclass(frozen=True)
class AuditReport:
    counts: dict[str, int]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class _BlobSpec:
    sha256: str
    size: int


def canonical_json(record: Any) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"повторный ключ {key!r}")
        result[key] = value
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _right_is_expired(record: dict[str, Any]) -> bool:
    expires = record.get("rights_expires_at")
    return bool(expires and date.fromisoformat(expires) < date.today())


class ManifestStore:
    """v1 только добавляет новые ID: точный повтор либо явный конфликт."""

    def __init__(
        self,
        *,
        project_root: Path,
        manifest_dir: Path | None = None,
        schema_dir: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.manifest_dir = Path(manifest_dir or self.project_root / "manifests").resolve()
        self.schema_dir = Path(
            schema_dir or self.project_root / "manifests" / "schemas"
        ).resolve()
        self.schemas = SchemaCatalog(self.schema_dir)

    def path_for(self, kind: str) -> Path:
        try:
            filename = REGISTRY_FILES[kind]
        except KeyError as exc:
            raise KeyError(f"Неизвестный реестр: {kind}") from exc
        return self.manifest_dir / filename

    def records(self, kind: str) -> list[dict[str, Any]]:
        """Прочитать реестр, проверив JSON Schema и уникальность ID."""

        return list(self._read_kind(kind).values())

    def preflight(
        self,
        plans: Iterable[ManifestPlan],
        *,
        allow_unresolved_rights: bool = False,
    ) -> CommitResult:
        """Проверить весь пакет без создания каталога, lock-файла и данных."""

        current = {kind: self._read_kind(kind) for kind in REGISTRY_FILES}
        new_records, unchanged, blob_state = self._analyze_plans(
            plans,
            current=current,
            allow_unresolved_rights=allow_unresolved_rights,
        )
        return CommitResult(
            inserted={kind: len(rows) for kind, rows in new_records.items()},
            unchanged=unchanged,
            written_blobs=blob_state["new"],
            unchanged_blobs=blob_state["unchanged"],
            dry_run=True,
        )

    def commit(self, plan: ManifestPlan, *, dry_run: bool = False) -> CommitResult:
        """Проверить план и безопасно добавить только новые строки."""

        if dry_run:
            return self.preflight([plan])

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            current = {kind: self._read_kind(kind) for kind in REGISTRY_FILES}
            new_records, unchanged, _blob_state = self._analyze_plans(
                [plan],
                current=current,
                allow_unresolved_rights=False,
            )
            written_blobs, unchanged_blobs = self._write_blobs(plan.blobs)
            # При сбое повтор того же плана завершит оставшиеся шаги без дублей.
            for kind in ("rights", "works", "artifacts"):
                self._atomic_append(self.path_for(kind), new_records[kind])

            return CommitResult(
                inserted={kind: len(rows) for kind, rows in new_records.items()},
                unchanged=unchanged,
                written_blobs=written_blobs,
                unchanged_blobs=unchanged_blobs,
                dry_run=False,
            )

    def _analyze_plans(
        self,
        plans: Iterable[ManifestPlan],
        *,
        current: dict[str, dict[str, dict[str, Any]]],
        allow_unresolved_rights: bool,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, int],
        dict[str, int],
    ]:
        candidate: dict[str, dict[str, dict[str, Any]]] = {
            kind: {} for kind in REGISTRY_FILES
        }
        blob_specs: dict[str, _BlobSpec] = {}

        for plan in plans:
            normalized = self._normalize_plan(plan)
            for kind in ("rights", "works", "artifacts"):
                for record_id, record in normalized[kind].items():
                    previous = candidate[kind].get(record_id)
                    if previous is not None and not self._records_equivalent(
                        kind, previous, record
                    ):
                        raise ManifestConflictError(
                            f"{kind}: пакет содержит разные записи с ID {record_id!r}"
                        )
                    candidate[kind].setdefault(record_id, record)

            for blob in plan.blobs:
                actual_sha = sha256_bytes(blob.data)
                if actual_sha != blob.sha256:
                    raise ManifestError(
                        f"blob {blob.relative_path}: заявлен SHA-256 {blob.sha256}, "
                        f"получен {actual_sha}"
                    )
                self._resolve_data_path(blob.relative_path)
                spec = _BlobSpec(blob.sha256, len(blob.data))
                previous_spec = blob_specs.get(blob.relative_path)
                if previous_spec is not None and previous_spec != spec:
                    raise ManifestConflictError(
                        f"Два разных blob претендуют на путь {blob.relative_path}"
                    )
                blob_specs[blob.relative_path] = spec

        new_records: dict[str, list[dict[str, Any]]] = {
            kind: [] for kind in REGISTRY_FILES
        }
        unchanged = {kind: 0 for kind in REGISTRY_FILES}
        for kind in ("rights", "works", "artifacts"):
            for record_id, record in candidate[kind].items():
                existing = current[kind].get(record_id)
                if existing is None:
                    new_records[kind].append(record)
                elif self._records_equivalent(kind, existing, record):
                    unchanged[kind] += 1
                else:
                    raise ManifestConflictError(
                        f"{kind}: ID {record_id!r} уже существует с другим содержимым"
                    )

        combined = {
            kind: {**current[kind], **candidate[kind]} for kind in REGISTRY_FILES
        }
        self._validate_relations(
            combined,
            allow_unresolved_rights=allow_unresolved_rights,
        )
        self._validate_parent_cycles(combined["artifacts"])
        blob_state = self._validate_artifact_payloads(
            combined["artifacts"],
            blob_specs,
        )
        return new_records, unchanged, blob_state

    def audit(self) -> AuditReport:
        """Проверить схемы, внешние ключи, хеши, пути и циклы родителей."""

        errors: list[str] = []
        warnings: list[str] = []
        current: dict[str, dict[str, dict[str, Any]]] = {}
        for kind in REGISTRY_FILES:
            try:
                current[kind] = self._read_kind(kind)
            except (ManifestError, SchemaValidationError) as exc:
                errors.append(str(exc))
                current[kind] = {}

        try:
            self._validate_relations(current)
        except ManifestError as exc:
            errors.extend(str(exc).splitlines())

        try:
            self._validate_artifact_payloads(current["artifacts"], {})
        except ManifestError as exc:
            errors.extend(str(exc).splitlines())

        artifact_ids: set[str] = set()
        for record in current["artifacts"].values():
            artifact_id = record.get("artifact_id")
            if artifact_id:
                if artifact_id in artifact_ids:
                    errors.append(f"artifacts: artifact_id {artifact_id!r} не уникален")
                artifact_ids.add(artifact_id)

            try:
                self._validate_timestamp_order("artifacts", record)
            except ManifestError as exc:
                errors.append(str(exc))

            if record.get("acquisition_status") == "retrieved" and not record.get(
                "rights_record_ids"
            ):
                warnings.append(
                    f"artifacts: {record['artifact_record_id']} получен, но права ещё не привязаны"
                )
            if record.get("acquisition_status") == "retrieved" and not record.get(
                "retrievals"
            ):
                warnings.append(
                    f"artifacts: {record['artifact_record_id']} не имеет точного события "
                    "получения; происхождение неполно"
                )

        for record in current["works"].values():
            try:
                self._validate_timestamp_order("works", record)
            except ManifestError as exc:
                errors.append(str(exc))

        for record in current["rights"].values():
            try:
                self._validate_timestamp_order("rights", record, updated_required=False)
            except ManifestError as exc:
                errors.append(str(exc))

        try:
            self._validate_parent_cycles(current["artifacts"])
        except ManifestError as exc:
            errors.append(str(exc))

        if not any(current.values()):
            errors.append(
                "Рабочие реестры works.jsonl, artifacts.jsonl и rights.jsonl отсутствуют "
                "или пусты"
            )

        return AuditReport(
            counts={kind: len(records) for kind, records in current.items()},
            errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _normalize_plan(
        self, plan: ManifestPlan
    ) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for kind in ("rights", "works", "artifacts"):
            primary = PRIMARY_FIELDS[kind]
            index: dict[str, dict[str, Any]] = {}
            for record in getattr(plan, kind):
                self.schemas.validate(kind, record)
                self._validate_timestamp_order(
                    kind,
                    record,
                    updated_required=kind != "rights",
                )
                record_id = record[primary]
                previous = index.get(record_id)
                if previous is not None and not self._records_equivalent(
                    kind, previous, record
                ):
                    raise ManifestConflictError(
                        f"{kind}: план содержит разные записи с ID {record_id!r}"
                    )
                index.setdefault(record_id, record)
            result[kind] = index
        return result

    @staticmethod
    def _records_equivalent(
        kind: str,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        """Время импорта не должно ломать идемпотентность тех же данных."""

        if kind == "rights":
            return left == right
        ignored = {"created_at", "updated_at"}
        left_semantic = {key: value for key, value in left.items() if key not in ignored}
        right_semantic = {key: value for key, value in right.items() if key not in ignored}
        return left_semantic == right_semantic

    def _read_kind(self, kind: str) -> dict[str, dict[str, Any]]:
        path = self.path_for(kind)
        if not path.exists():
            return {}
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ManifestError(f"{path}: отсутствует конечный перевод строки")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(f"{path}: файл не является корректным UTF-8: {exc}") from exc

        primary = PRIMARY_FIELDS[kind]
        index: dict[str, dict[str, Any]] = {}
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
            except (json.JSONDecodeError, _DuplicateJsonKeyError) as exc:
                raise ManifestError(f"{path}:{line_number}: некорректный JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ManifestError(f"{path}:{line_number}: строка должна быть объектом")
            try:
                self.schemas.validate(kind, record)
            except SchemaValidationError as exc:
                raise SchemaValidationError(f"{path}:{line_number}: {exc}") from exc
            record_id = record[primary]
            if record_id in index:
                raise ManifestConflictError(
                    f"{path}:{line_number}: повторный {primary}={record_id!r}"
                )
            index[record_id] = record
        return index

    def _validate_relations(
        self,
        records: dict[str, dict[str, dict[str, Any]]],
        *,
        allow_unresolved_rights: bool = False,
    ) -> None:
        errors: list[str] = []
        works = records["works"]
        rights = records["rights"]
        artifacts = records["artifacts"]
        superseded_rights = self._validate_rights_supersedes(rights, errors)
        by_artifact_id = {
            record["artifact_id"]: record
            for record in artifacts.values()
            if record.get("artifact_id")
        }

        if len(by_artifact_id) != sum(
            1 for record in artifacts.values() if record.get("artifact_id")
        ):
            errors.append("artifacts: найден повторный artifact_id")

        self._validate_work_identity_uniqueness(works, errors)
        retrieval_ids: set[str] = set()

        for artifact in artifacts.values():
            record_id = artifact["artifact_record_id"]
            work = works.get(artifact["work_id"])
            if work is None:
                errors.append(
                    f"artifacts: {record_id} ссылается на отсутствующий work_id "
                    f"{artifact['work_id']!r}"
                )
                continue

            parent_id = artifact.get("parent_artifact_id")
            if parent_id and parent_id not in by_artifact_id:
                errors.append(
                    f"artifacts: {record_id} ссылается на отсутствующего родителя {parent_id!r}"
                )
            elif parent_id and by_artifact_id[parent_id]["work_id"] != artifact["work_id"]:
                errors.append(f"artifacts: {record_id} и его родитель относятся к разным работам")

            if (
                artifact.get("extraction_version") == "legacy-import-v1"
                and artifact.get("content_role") == "full_text"
                and artifact.get("representation") in {"plain_text", "ocr_text"}
            ):
                parent = by_artifact_id.get(parent_id) if parent_id else None
                permitted_methods = {"legacy_pdf", "legacy_pdf_ocr_layout"}
                if (
                    parent is None
                    or parent.get("representation") != "pdf"
                    or artifact.get("extraction_method") not in permitted_methods
                ):
                    errors.append(
                        f"artifacts: {record_id}: legacy full_text не имеет "
                        "подтверждённого PDF-происхождения"
                    )

            referenced_rights: list[dict[str, Any]] = []
            for rights_id in artifact.get("rights_record_ids", []):
                rights_record = rights.get(rights_id)
                if rights_record is None:
                    errors.append(
                        f"artifacts: {record_id} ссылается на отсутствующий rights_record_id "
                        f"{rights_id!r}"
                    )
                elif not self._rights_apply(rights_record, work, artifact):
                    errors.append(
                        f"artifacts: право {rights_id!r} не применимо к {record_id}"
                    )
                elif (
                    rights_record["operation"] == "acquisition"
                    and not self._right_matches_operation_context(
                        rights_record,
                        artifact,
                        "acquisition",
                    )
                ):
                    errors.append(
                        f"artifacts: право {rights_id!r} не соответствует "
                        f"режиму {artifact['acquisition_method']}|"
                        f"{artifact['acquisition_scope']} артефакта {record_id}"
                    )
                else:
                    referenced_rights.append(rights_record)

            for retrieval in artifact.get("retrievals", []):
                retrieval_id = retrieval["retrieval_id"]
                if retrieval_id in retrieval_ids:
                    errors.append(f"artifacts: повторный retrieval_id {retrieval_id!r}")
                retrieval_ids.add(retrieval_id)

            if not referenced_rights and allow_unresolved_rights:
                continue
            active_rights = [
                item
                for item in referenced_rights
                if item["rights_record_id"] not in superseded_rights
            ]
            all_active_applicable_rights = [
                item
                for item in rights.values()
                if item["rights_record_id"] not in superseded_rights
                and self._rights_apply(item, work, artifact)
            ]
            if artifact["acquisition_status"] in {"ready", "retrieved"}:
                self._require_permitting_right(
                    active_rights,
                    all_active_applicable_rights,
                    operation="acquisition",
                    artifact=artifact,
                    artifact_record_id=record_id,
                    errors=errors,
                )
            elif artifact["acquisition_status"] == "rights_blocked":
                decisions = self._operation_decisions(
                    all_active_applicable_rights,
                    operation="acquisition",
                    artifact=artifact,
                )
                referenced_ids = {
                    item["rights_record_id"] for item in active_rights
                }
                if not decisions:
                    errors.append(
                        f"artifacts: {record_id} имеет rights_blocked без записи acquisition"
                    )
                elif not any(
                    item["rights_record_id"] in referenced_ids for item in decisions
                ):
                    errors.append(
                        f"artifacts: {record_id} не ссылается на определяющую "
                        "запись acquisition"
                    )
                elif all(self._right_permits(item) for item in decisions):
                    errors.append(
                        f"artifacts: {record_id} имеет rights_blocked, но право acquisition "
                        "разрешает операцию"
                    )

            # Любой path означает факт локального хранения, даже если
            # извлечение и обработка ещё не начаты.
            if artifact.get("path") is not None:
                self._require_permitting_right(
                    active_rights,
                    all_active_applicable_rights,
                    operation="storage",
                    artifact=artifact,
                    artifact_record_id=record_id,
                    errors=errors,
                )

        if errors:
            raise ManifestError("\n".join(errors))

    def _require_permitting_right(
        self,
        rights: list[dict[str, Any]],
        all_applicable_rights: list[dict[str, Any]],
        *,
        operation: str,
        artifact: dict[str, Any],
        artifact_record_id: str,
        errors: list[str],
    ) -> None:
        decisions = self._operation_decisions(
            all_applicable_rights,
            operation=operation,
            artifact=artifact,
        )
        if not decisions:
            errors.append(
                f"artifacts: {artifact_record_id} не имеет применимой записи {operation}"
            )
            return
        referenced_ids = {item["rights_record_id"] for item in rights}
        if not any(item["rights_record_id"] in referenced_ids for item in decisions):
            errors.append(
                f"artifacts: {artifact_record_id} не ссылается на наиболее "
                f"конкретную запись {operation}"
            )
        if not all(self._right_permits(item) for item in decisions):
            details: set[str] = set()
            for item in decisions:
                detail = item["status"]
                if _right_is_expired(item):
                    detail += ":expired"
                elif item["status"] == "conditional" and not self._conditions_satisfied(item):
                    detail += ":conditions_pending"
                details.add(detail)
            errors.append(
                f"artifacts: {artifact_record_id}: операция {operation} не разрешена "
                f"определяющей записью прав (статусы: "
                f"{', '.join(sorted(details))})"
            )

    @staticmethod
    def _conditions_satisfied(right: dict[str, Any]) -> bool:
        # Минимальное материализованное поле v1. До реального заполнения
        # DEC-013 должен заменить его журналом выполнения каждого условия.
        return bool(
            right.get("conditions_satisfied_at")
            and right.get("conditions_evidence_sha256")
        )

    @classmethod
    def _right_permits(cls, right: dict[str, Any]) -> bool:
        if _right_is_expired(right):
            return False
        if right["status"] == "allowed":
            return True
        return right["status"] == "conditional" and cls._conditions_satisfied(right)

    @staticmethod
    def _right_matches_operation_context(
        right: dict[str, Any],
        artifact: dict[str, Any],
        operation: str,
    ) -> bool:
        method = right["acquisition_method"]
        scope = right["acquisition_scope"]
        if operation != "acquisition" and method is None and scope is None:
            return True
        return (
            method == artifact.get("acquisition_method")
            and scope == artifact.get("acquisition_scope")
        )

    @classmethod
    def _operation_decisions(
        cls,
        rights: list[dict[str, Any]],
        *,
        operation: str,
        artifact: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates = [
            item
            for item in rights
            if item["operation"] == operation
            and cls._right_matches_operation_context(item, artifact, operation)
        ]
        if not candidates:
            return []

        def specificity(item: dict[str, Any]) -> tuple[int, int]:
            mode_specificity = int(item["acquisition_method"] is not None)
            return SCOPE_SPECIFICITY[item["scope_type"]], mode_specificity

        controlling_specificity = max(specificity(item) for item in candidates)
        return [
            item
            for item in candidates
            if specificity(item) == controlling_specificity
        ]

    @staticmethod
    def _validate_rights_supersedes(
        rights: dict[str, dict[str, Any]],
        errors: list[str],
    ) -> set[str]:
        superseded: set[str] = set()
        graph: dict[str, str] = {}
        successor_by_previous: dict[str, str] = {}
        for record_id, record in rights.items():
            previous = record.get("supersedes_rights_record_id")
            if not previous:
                continue
            if previous not in rights:
                errors.append(
                    f"rights: {record_id} ссылается на отсутствующую запись {previous!r}"
                )
                continue
            previous_record = rights[previous]
            comparable_fields = (
                "scope_type",
                "scope_id",
                "operation",
                "acquisition_method",
                "acquisition_scope",
            )
            if any(record[field] != previous_record[field] for field in comparable_fields):
                errors.append(
                    f"rights: {record_id} заменяет {previous!r} с другой областью, "
                    "операцией или режимом получения"
                )
                continue
            if previous == record_id:
                errors.append(f"rights: {record_id} не может заменять сам себя")
                continue
            previous_successor = successor_by_previous.get(previous)
            if previous_successor is not None:
                errors.append(
                    f"rights: {previous!r} имеет два преемника: "
                    f"{previous_successor!r} и {record_id!r}"
                )
                continue
            if date.fromisoformat(record["rights_checked_at"]) < date.fromisoformat(
                previous_record["rights_checked_at"]
            ):
                errors.append(
                    f"rights: {record_id} не может заменить более позднюю "
                    f"проверку {previous!r}"
                )
                continue
            created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            previous_created = datetime.fromisoformat(
                previous_record["created_at"].replace("Z", "+00:00")
            )
            if created <= previous_created:
                errors.append(
                    f"rights: {record_id} должен быть создан позже "
                    f"заменяемой записи {previous!r}"
                )
                continue
            superseded.add(previous)
            graph[record_id] = previous
            successor_by_previous[previous] = record_id

        for start in graph:
            seen: set[str] = set()
            current: str | None = start
            while current in graph:
                if current in seen:
                    errors.append(f"rights: цикл supersedes около {current!r}")
                    break
                seen.add(current)
                current = graph[current]
        return superseded

    @staticmethod
    def _validate_work_identity_uniqueness(
        works: dict[str, dict[str, Any]],
        errors: list[str],
    ) -> None:
        for field in ("doi", "edn", "canonical_url"):
            owners: dict[str, str] = {}
            for work_id, work in works.items():
                value = work.get(field)
                if not value:
                    continue
                normalized = str(value).casefold()
                previous = owners.get(normalized)
                if previous and previous != work_id:
                    errors.append(
                        f"works: {field}={value!r} принадлежит одновременно {previous!r} "
                        f"и {work_id!r}"
                    )
                owners[normalized] = work_id

        identity_owners = {work_id: work_id for work_id in works}
        duplicate_graph: dict[str, str] = {}
        for work_id, work in works.items():
            for alias in work.get("work_aliases", []):
                owner = identity_owners.get(alias)
                if owner and owner != work_id:
                    errors.append(
                        f"works: псевдоним {alias!r} принадлежит одновременно {owner!r} "
                        f"и {work_id!r}"
                    )
                identity_owners[alias] = work_id

            duplicate_of = work.get("duplicate_of_work_id")
            if not duplicate_of:
                continue
            if duplicate_of == work_id:
                errors.append(f"works: {work_id!r} не может быть дубликатом самого себя")
            elif duplicate_of not in works:
                errors.append(
                    f"works: {work_id!r} ссылается на отсутствующий duplicate_of_work_id "
                    f"{duplicate_of!r}"
                )
            else:
                duplicate_graph[work_id] = duplicate_of

        for start in duplicate_graph:
            seen: set[str] = set()
            current: str | None = start
            while current in duplicate_graph:
                if current in seen:
                    errors.append(f"works: цикл duplicate_of_work_id около {current!r}")
                    break
                seen.add(current)
                current = duplicate_graph[current]

    @staticmethod
    def _rights_apply(
        rights: dict[str, Any], work: dict[str, Any], artifact: dict[str, Any]
    ) -> bool:
        scope_type = rights["scope_type"]
        scope_id = rights["scope_id"]
        expected = {
            "source_group": work["source_group_id"],
            "source": work["source_id"],
            "journal": work["journal_id"],
            "work": work["work_id"],
        }
        if scope_type == "artifact":
            return scope_id in {artifact["artifact_record_id"], artifact.get("artifact_id")}
        if scope_type == "work":
            return scope_id in {work["work_id"], *work.get("work_aliases", [])}
        return expected[scope_type] == scope_id

    def _validate_artifact_payloads(
        self,
        artifacts: dict[str, dict[str, Any]],
        blob_specs: dict[str, _BlobSpec],
    ) -> dict[str, int]:
        artifact_paths: dict[str, dict[str, Any]] = {}
        for artifact in artifacts.values():
            record_id = artifact["artifact_record_id"]
            artifact_id = artifact.get("artifact_id")
            digest = artifact.get("sha256")
            if artifact_id is not None and artifact_id != f"sha256:{digest}":
                raise ManifestError(
                    f"artifacts: {record_id}: artifact_id не соответствует полю sha256"
                )

            path_value = artifact.get("path")
            if path_value is None:
                continue
            previous = artifact_paths.get(path_value)
            if previous is not None and previous["artifact_record_id"] != record_id:
                raise ManifestConflictError(
                    f"artifacts: путь {path_value!r} назначен нескольким записям"
                )
            artifact_paths[path_value] = artifact

            spec = blob_specs.get(path_value)
            if spec is not None:
                if artifact.get("sha256") != spec.sha256 or artifact.get("bytes") != spec.size:
                    raise ManifestError(
                        f"artifacts: {record_id}: запланированные байты не совпадают с реестром"
                    )
            else:
                self._validate_artifact_file(artifact)

        for path_value in blob_specs:
            if path_value not in artifact_paths:
                raise ManifestError(f"blob {path_value} не связан ни с одним артефактом")

        state = {"new": 0, "unchanged": 0}
        for path_value, spec in blob_specs.items():
            target = self._resolve_data_path(path_value)
            if not target.exists():
                state["new"] += 1
            elif self._file_matches(target, spec):
                state["unchanged"] += 1
            else:
                raise ManifestConflictError(
                    f"Путь {path_value} уже занят другими байтами"
                )
        return state

    def _write_blobs(self, blobs: list[PlannedBlob]) -> tuple[int, int]:
        unique: dict[str, PlannedBlob] = {}
        for blob in blobs:
            actual_sha = sha256_bytes(blob.data)
            if actual_sha != blob.sha256:
                raise ManifestError(
                    f"blob {blob.relative_path}: заявлен SHA-256 {blob.sha256}, "
                    f"получен {actual_sha}"
                )
            previous = unique.get(blob.relative_path)
            if previous is not None and (
                previous.sha256 != blob.sha256 or previous.data != blob.data
            ):
                raise ManifestConflictError(
                    f"Два разных blob претендуют на путь {blob.relative_path}"
                )
            unique[blob.relative_path] = blob
        written = unchanged = 0
        for blob in unique.values():
            target = self._resolve_data_path(blob.relative_path)
            if target.exists():
                if not self._file_matches(target, _BlobSpec(blob.sha256, len(blob.data))):
                    raise ManifestConflictError(
                        f"Путь {blob.relative_path} занят другими байтами"
                    )
                unchanged += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(blob.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # link создаёт конечное имя атомарно и никогда не
                    # перезаписывает уже существующий неизменяемый объект.
                    os.link(temp_name, target)
                except FileExistsError:
                    if not self._file_matches(
                        target, _BlobSpec(blob.sha256, len(blob.data))
                    ):
                        raise ManifestConflictError(
                            f"Путь {blob.relative_path} конкурентно занят другими байтами"
                        )
                    unchanged += 1
                    continue
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            written += 1
        return written, unchanged

    @staticmethod
    def _file_matches(path: Path, spec: _BlobSpec) -> bool:
        return (
            path.is_file()
            and path.stat().st_size == spec.size
            and sha256_file(path) == spec.sha256
        )

    def _resolve_data_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ManifestError(f"Путь артефакта должен быть относительным: {relative_path}")
        resolved = (self.project_root / relative).resolve()
        data_root = (self.project_root / "data").resolve()
        if not resolved.is_relative_to(data_root):
            raise ManifestError(f"Путь артефакта выходит за data/: {relative_path}")
        return resolved

    def _validate_artifact_file(self, artifact: dict[str, Any]) -> None:
        path_value = artifact.get("path")
        if path_value is None:
            return
        path = self._resolve_data_path(path_value)
        record_id = artifact["artifact_record_id"]
        if not path.is_file():
            raise ManifestError(f"artifacts: {record_id}: файл не найден: {path_value}")
        actual_size = path.stat().st_size
        if actual_size != artifact["bytes"]:
            raise ManifestError(
                f"artifacts: {record_id}: размер {actual_size}, в реестре {artifact['bytes']}"
            )
        actual_sha = sha256_file(path)
        if actual_sha != artifact["sha256"]:
            raise ManifestError(
                f"artifacts: {record_id}: SHA-256 файла не совпадает с реестром"
            )
        if artifact["artifact_id"] != f"sha256:{actual_sha}":
            raise ManifestError(
                f"artifacts: {record_id}: artifact_id не соответствует SHA-256"
            )

    @staticmethod
    def _validate_timestamp_order(
        kind: str,
        record: dict[str, Any],
        *,
        updated_required: bool = True,
    ) -> None:
        created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        if kind == "rights":
            record_id = record[PRIMARY_FIELDS[kind]]
            checked = date.fromisoformat(record["rights_checked_at"])
            now = datetime.now(tz=created.tzinfo)
            if created > now:
                raise ManifestError(f"rights: {record_id}: created_at находится в будущем")
            if checked > now.date():
                raise ManifestError(
                    f"rights: {record_id}: rights_checked_at находится в будущем"
                )
            if checked > created.date():
                raise ManifestError(
                    f"rights: {record_id}: rights_checked_at позже created_at"
                )
            satisfied_at = record.get("conditions_satisfied_at")
            if satisfied_at:
                satisfied = datetime.fromisoformat(satisfied_at.replace("Z", "+00:00"))
                if satisfied > created:
                    raise ManifestError(
                        f"rights: {record_id}: conditions_satisfied_at позже created_at"
                    )
            return
        if not updated_required:
            return
        updated = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
        if created > updated:
            record_id = record[PRIMARY_FIELDS[kind]]
            raise ManifestError(f"{kind}: {record_id}: created_at позже updated_at")

    @staticmethod
    def _validate_parent_cycles(artifacts: dict[str, dict[str, Any]]) -> None:
        by_artifact_id = {
            record["artifact_id"]: record
            for record in artifacts.values()
            if record.get("artifact_id")
        }
        for artifact_id in by_artifact_id:
            seen: set[str] = set()
            current = artifact_id
            while current:
                if current in seen:
                    raise ManifestError(f"artifacts: цикл parent_artifact_id около {current}")
                seen.add(current)
                parent = by_artifact_id.get(current, {}).get("parent_artifact_id")
                current = parent

    @staticmethod
    def _atomic_append(path: Path, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_bytes() if path.exists() else b""
        if existing and not existing.endswith(b"\n"):
            raise ManifestError(f"{path}: отсутствует конечный перевод строки")
        addition = "".join(f"{canonical_json(record)}\n" for record in records).encode(
            "utf-8"
        )
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(existing)
                stream.write(addition)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        lock_path = self.manifest_dir / ".registry.lock"
        with lock_path.open("a+b") as lock_file:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - проект запускается на Unix/macOS
                yield
                return
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
