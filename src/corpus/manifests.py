"""Безопасная пакетная запись и семантический аудит реестров корпуса."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .identity import canonicalize_url, normalize_doi
from .schema_validation import SchemaCatalog, SchemaValidationError

SNAPSHOT_REGISTRY_FILES = {
    "works": "works.jsonl",
    "artifacts": "artifacts.jsonl",
}
HISTORY_REGISTRY_FILES = {
    "rights": "rights.jsonl",
    "work_revisions": "work_revisions.jsonl",
    "artifact_revisions": "artifact_revisions.jsonl",
    "retrieval_events": "retrieval_events.jsonl",
    "work_aliases": "work_aliases.jsonl",
    "identity_conflicts": "identity_conflicts.jsonl",
    "operation_decisions": "operation_decisions.jsonl",
    "condition_fulfilments": "condition_fulfilments.jsonl",
}
REGISTRY_FILES = {**SNAPSHOT_REGISTRY_FILES, **HISTORY_REGISTRY_FILES}
PRIMARY_FIELDS = {
    "works": "work_id",
    "artifacts": "artifact_record_id",
    "rights": "rights_record_id",
    "work_revisions": "work_revision_id",
    "artifact_revisions": "artifact_revision_id",
    "retrieval_events": "retrieval_id",
    "work_aliases": "alias_record_id",
    "identity_conflicts": "conflict_id",
    "operation_decisions": "decision_id",
    "condition_fulfilments": "fulfilment_id",
}
PLAN_HISTORY_FIELDS = (
    "rights",
    "retrieval_events",
    "work_aliases",
    "identity_conflicts",
    "operation_decisions",
    "condition_fulfilments",
)
REVISION_KIND_BY_SNAPSHOT = {
    "works": "work_revisions",
    "artifacts": "artifact_revisions",
}
REVISION_ENTITY_FIELD = {
    "works": "work_id",
    "artifacts": "artifact_record_id",
}
FREEZE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROJECT_SUBJECT_ID = "ruphysbert"
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


class ManifestConcurrencyError(ManifestConflictError):
    """Текущий снимок реестра изменился параллельно с планом."""


class _DuplicateJsonKeyError(ValueError):
    """JSON-объект содержит один ключ более одного раза."""


@dataclass(frozen=True)
class PlannedBlob:
    """Неизменяемые байты, которые нужно положить в data/."""

    relative_path: str
    data: bytes
    sha256: str


@dataclass
class ManifestPlan:
    """Пакет реестровых записей и файлов для совместной проверки."""

    rights: list[dict[str, Any]] = field(default_factory=list)
    works: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    retrieval_events: list[dict[str, Any]] = field(default_factory=list)
    work_aliases: list[dict[str, Any]] = field(default_factory=list)
    identity_conflicts: list[dict[str, Any]] = field(default_factory=list)
    operation_decisions: list[dict[str, Any]] = field(default_factory=list)
    condition_fulfilments: list[dict[str, Any]] = field(default_factory=list)
    work_update_reasons: dict[str, str] = field(default_factory=dict)
    artifact_update_reasons: dict[str, str] = field(default_factory=dict)
    blobs: list[PlannedBlob] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommitResult:
    """Сводные счётчики предварительной проверки или записи плана."""

    inserted: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]
    written_blobs: int
    unchanged_blobs: int
    snapshot_sha256: dict[str, str]
    dry_run: bool


@dataclass(frozen=True)
class AuditReport:
    """Результат полного аудита рабочих реестров и файлов корпуса."""

    counts: dict[str, int]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Показать, завершился ли аудит без ошибок."""

        return not self.errors


@dataclass(frozen=True)
class FreezeResult:
    """Описание созданной неизменяемой версии реестров."""

    version: str
    path: Path
    files: dict[str, str]
    manifest_sha256: str


@dataclass(frozen=True)
class FrozenReport:
    """Результат проверки ранее зафиксированной версии реестров."""

    version: str
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Показать, совпадают ли файлы версии с паспортом фиксации."""

        return not self.errors


@dataclass(frozen=True)
class _BlobSpec:
    """Ожидаемые хеш и размер одного файлового объекта."""

    sha256: str
    size: int


@dataclass
class _AnalyzedCommit:
    """Полностью проверенное состояние, готовое к записи."""

    snapshots: dict[str, dict[str, dict[str, Any]]]
    history_additions: dict[str, list[dict[str, Any]]]
    inserted: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]
    blob_state: dict[str, int]


def canonical_json(record: Any) -> str:
    """Сериализовать значение в детерминированное компактное представление JSON."""

    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Собрать JSON-объект и отклонить повторяющиеся ключи."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"повторный ключ {key!r}")

        result[key] = value

    return result


def sha256_bytes(data: bytes) -> str:
    """Вычислить шестнадцатеричный SHA-256 для переданных байтов."""

    return hashlib.sha256(data).hexdigest()


def _record_sha256(record: dict[str, Any]) -> str:
    """Вычислить SHA-256 канонического представления одной записи."""

    return sha256_bytes(canonical_json(record).encode("utf-8"))


def _snapshot_bytes(
    kind: str,
    records: dict[str, dict[str, Any]],
) -> bytes:
    """Сериализовать полный снимок в устойчивом порядке первичных ключей."""

    if kind not in SNAPSHOT_REGISTRY_FILES:
        raise KeyError(f"Реестр {kind!r} не является текущим снимком")

    return "".join(
        f"{canonical_json(records[record_id])}\n"
        for record_id in sorted(records)
    ).encode("utf-8")


def _history_bytes(records: dict[str, dict[str, Any]]) -> bytes:
    """Сериализовать историю детерминированно для вычисления её хеша."""

    return "".join(
        f"{canonical_json(records[record_id])}\n"
        for record_id in sorted(records)
    ).encode("utf-8")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Вычислить SHA-256 файла, читая его блоками заданного размера."""

    if chunk_size <= 0:
        raise ValueError("chunk_size должен быть больше нуля")

    digest = hashlib.sha256()

    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)

    return digest.hexdigest()


def _right_is_expired(record: dict[str, Any]) -> bool:
    """Проверить, истёк ли срок действия записи о правах."""

    expires = record.get("rights_expires_at")
    return bool(expires and date.fromisoformat(expires) < date.today())


class ManifestStore:
    """Хранилище текущих снимков и неизменяемой истории корпуса."""

    def __init__(
        self,
        *,
        project_root: Path,
        manifest_dir: Path | None = None,
        schema_dir: Path | None = None,
    ) -> None:
        """Настроить пути проекта, реестров и их JSON Schema."""

        self.project_root = Path(project_root).resolve()
        self.manifest_dir = Path(
            manifest_dir or self.project_root / "manifests"
        ).resolve()

        self.schema_dir = Path(
            schema_dir or self.project_root / "manifests" / "schemas"
        ).resolve()

        self.schemas = SchemaCatalog(self.schema_dir)

    def path_for(self, kind: str) -> Path:
        """Вернуть путь к JSONL-файлу реестра выбранного вида."""

        try:
            filename = REGISTRY_FILES[kind]

        except KeyError as exception:
            raise KeyError(f"Неизвестный реестр: {kind}") from exception

        return self.manifest_dir / filename

    def records(self, kind: str) -> list[dict[str, Any]]:
        """Прочитать реестр, проверив JSON Schema и уникальность ID."""

        return list(self._read_kind(kind).values())

    def snapshot_sha256(self, kind: str) -> str:
        """Вернуть детерминированный SHA-256 одного текущего снимка."""

        records = self._read_kind(kind)

        return sha256_bytes(_snapshot_bytes(kind, records))

    def snapshot_hashes(self) -> dict[str, str]:
        """Вернуть SHA-256 всех изменяемых текущих снимков."""

        return {
            kind: self.snapshot_sha256(kind)
            for kind in SNAPSHOT_REGISTRY_FILES
        }

    def preflight(
        self,
        plans: Iterable[ManifestPlan],
        *,
        allow_unresolved_rights: bool = False,
        expected_snapshot_hashes: dict[str, str] | None = None,
    ) -> CommitResult:
        """Проверить весь пакет без создания каталога, lock-файла и данных."""

        current = {kind: self._read_kind(kind) for kind in REGISTRY_FILES}

        analysis = self._analyze_plans(
            plans,
            current=current,
            allow_unresolved_rights=allow_unresolved_rights,
            expected_snapshot_hashes=expected_snapshot_hashes,
        )

        return CommitResult(
            inserted=analysis.inserted,
            updated=analysis.updated,
            unchanged=analysis.unchanged,
            written_blobs=analysis.blob_state["new"],
            unchanged_blobs=analysis.blob_state["unchanged"],
            snapshot_sha256=self._hash_analyzed_snapshots(analysis.snapshots),
            dry_run=True,
        )

    def commit(
        self,
        plan: ManifestPlan,
        *,
        dry_run: bool = False,
        expected_snapshot_hashes: dict[str, str] | None = None,
    ) -> CommitResult:
        """Проверить и записать план с защитой обновлений по хешу снимка."""

        if dry_run:
            return self.preflight(
                [plan],
                expected_snapshot_hashes=expected_snapshot_hashes,
            )

        self.manifest_dir.mkdir(parents=True, exist_ok=True)

        with self._exclusive_lock():
            current = {kind: self._read_kind(kind) for kind in REGISTRY_FILES}
            analysis = self._analyze_plans(
                [plan],
                current=current,
                allow_unresolved_rights=False,
                expected_snapshot_hashes=expected_snapshot_hashes,
            )

            changed_registry_paths = {
                self.path_for(kind)
                for kind in HISTORY_REGISTRY_FILES
                if analysis.history_additions[kind]
            }
            changed_registry_paths.update(
                self.path_for(kind)
                for kind in SNAPSHOT_REGISTRY_FILES
                if analysis.inserted[kind] or analysis.updated[kind]
            )
            original_registry_bytes = {
                path: path.read_bytes() if path.exists() else None
                for path in changed_registry_paths
            }

            new_blob_paths: list[Path] = []

            try:
                written_blobs, unchanged_blobs = self._write_blobs(
                    plan.blobs,
                    created_paths=new_blob_paths,
                )

                for kind in HISTORY_REGISTRY_FILES:
                    self._atomic_append(
                        self.path_for(kind),
                        analysis.history_additions[kind],
                    )

                for kind in SNAPSHOT_REGISTRY_FILES:
                    if analysis.inserted[kind] or analysis.updated[kind]:
                        self._atomic_replace_snapshot(
                            self.path_for(kind),
                            kind,
                            analysis.snapshots[kind],
                        )

            except Exception:
                # Между несколькими файлами нет общей операции os.replace.
                # При штатной ошибке восстанавливаем весь набор реестров.
                try:
                    self._restore_registry_files(original_registry_bytes)

                finally:
                    self._remove_new_blobs(new_blob_paths)

                raise

            return CommitResult(
                inserted=analysis.inserted,
                updated=analysis.updated,
                unchanged=analysis.unchanged,
                written_blobs=written_blobs,
                unchanged_blobs=unchanged_blobs,
                snapshot_sha256=self._hash_analyzed_snapshots(analysis.snapshots),
                dry_run=False,
            )

    def freeze(
        self,
        version: str,
        expected_snapshot_hashes: dict[str, str],
    ) -> FreezeResult:
        """Проверить и атомарно зафиксировать неизменяемую версию реестров."""

        self._validate_freeze_version(version)

        missing_hashes = set(SNAPSHOT_REGISTRY_FILES) - set(
            expected_snapshot_hashes
        )

        if missing_hashes:
            names = ", ".join(sorted(missing_hashes))
            raise ManifestConflictError(
                f"Для фиксации нужны ожидаемые хеши снимков: {names}"
            )

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        frozen_root = self.manifest_dir / "frozen"
        frozen_root.mkdir(parents=True, exist_ok=True)
        destination = frozen_root / version

        with self._exclusive_lock():
            if destination.exists():
                raise ManifestConflictError(
                    f"Замороженная версия {version!r} уже существует"
                )

            current = {kind: self._read_kind(kind) for kind in REGISTRY_FILES}
            self._validate_expected_snapshot_hashes(
                current,
                updated={kind: 0 for kind in REGISTRY_FILES},
                expected=expected_snapshot_hashes,
            )
            report = self.audit()

            if not report.ok:
                details = "; ".join(report.errors)
                raise ManifestError(
                    f"Нельзя зафиксировать непроверенные реестры: {details}"
                )

            registry_bytes = {
                kind: (
                    _snapshot_bytes(kind, current[kind])
                    if kind in SNAPSHOT_REGISTRY_FILES
                    else _history_bytes(current[kind])
                )
                for kind in REGISTRY_FILES
            }
            registry_hashes = {
                kind: sha256_bytes(data) for kind, data in registry_bytes.items()
            }
            files = [
                {
                    "path": REGISTRY_FILES[kind],
                    "sha256": registry_hashes[kind],
                    "bytes": len(registry_bytes[kind]),
                    "records": len(current[kind]),
                }
                for kind in REGISTRY_FILES
            ]
            manifest = {
                "schema_version": "frozen-manifest-v1",
                "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_hashes": registry_hashes,
                "files": files,
            }
            self.schemas.validate("frozen_manifest", manifest)
            manifest_bytes = f"{canonical_json(manifest)}\n".encode("utf-8")
            manifest_sha256 = sha256_bytes(manifest_bytes)
            temporary_path = Path(
                tempfile.mkdtemp(prefix=f".{version}.", dir=frozen_root)
            )

            try:
                for kind, data in registry_bytes.items():
                    (temporary_path / REGISTRY_FILES[kind]).write_bytes(data)

                (temporary_path / "freeze_manifest.json").write_bytes(
                    manifest_bytes
                )
                (temporary_path / "freeze_manifest.sha256").write_text(
                    f"{manifest_sha256}  freeze_manifest.json\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, destination)

            finally:
                if temporary_path.exists():
                    shutil.rmtree(temporary_path)

        return FreezeResult(
            version=version,
            path=destination,
            files={item["path"]: item["sha256"] for item in files},
            manifest_sha256=manifest_sha256,
        )

    def verify_frozen(self, version: str) -> FrozenReport:
        """Сверить файлы замороженной версии с её паспортом и хешем."""

        self._validate_freeze_version(version)
        directory = self.manifest_dir / "frozen" / version
        errors: list[str] = []

        if not directory.is_dir():
            return FrozenReport(
                version=version,
                errors=(f"Замороженная версия {version!r} не найдена",),
            )

        manifest_path = directory / "freeze_manifest.json"
        checksum_path = directory / "freeze_manifest.sha256"

        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(
                manifest_bytes,
                object_pairs_hook=_object_without_duplicate_keys,
            )
            self.schemas.validate("frozen_manifest", manifest)

        except (
            OSError,
            json.JSONDecodeError,
            _DuplicateJsonKeyError,
            SchemaValidationError,
        ) as exception:
            return FrozenReport(
                version=version,
                errors=(f"Некорректный паспорт фиксации: {exception}",),
            )

        if manifest["version"] != version:
            errors.append("Версия в паспорте не совпадает с каталогом")

        try:
            checksum_text = checksum_path.read_text(encoding="utf-8")
            expected_manifest_sha256 = sha256_bytes(manifest_bytes)
            expected_checksum_text = (
                f"{expected_manifest_sha256}  freeze_manifest.json\n"
            )

        except OSError as exception:
            errors.append(f"Не удалось прочитать хеш паспорта: {exception}")

        else:
            if checksum_text != expected_checksum_text:
                errors.append("Файл SHA-256 паспорта фиксации изменён")

        listed_paths = {item["path"] for item in manifest["files"]}
        required_paths = set(REGISTRY_FILES.values())

        if (
            listed_paths != required_paths
            or len(listed_paths) != len(manifest["files"])
        ):
            errors.append("Паспорт содержит неполный или лишний набор реестров")

        expected_directory_paths = {
            *required_paths,
            "freeze_manifest.json",
            "freeze_manifest.sha256",
        }
        actual_directory_paths = {path.name for path in directory.iterdir()}

        if actual_directory_paths != expected_directory_paths:
            errors.append(
                "Каталог фиксации содержит лишние или неполные файлы"
            )

        for item in manifest["files"]:
            path = directory / item["path"]
            kind = next(
                (
                    registry_kind
                    for registry_kind, filename in REGISTRY_FILES.items()
                    if filename == item["path"]
                ),
                None,
            )

            if (
                kind is not None
                and manifest["snapshot_hashes"][kind] != item["sha256"]
            ):
                errors.append(
                    f"Хеш {item['path']} расходится между разделами паспорта"
                )

            if path.parent != directory or not path.is_file() or path.is_symlink():
                errors.append(f"Файл фиксации отсутствует или небезопасен: {item['path']}")
                continue

            data = path.read_bytes()

            if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
                errors.append(f"Файл фиксации изменён: {item['path']}")

        return FrozenReport(version=version, errors=tuple(errors))

    @staticmethod
    def _validate_freeze_version(version: str) -> None:
        """Отклонить пустое или небезопасное имя версии."""

        if not FREEZE_VERSION_PATTERN.fullmatch(version):
            raise ValueError(
                "Версия может содержать только буквы, цифры, точку, '_' и '-'"
            )

    def _analyze_plans(
        self,
        plans: Iterable[ManifestPlan],
        *,
        current: dict[str, dict[str, dict[str, Any]]],
        allow_unresolved_rights: bool,
        expected_snapshot_hashes: dict[str, str] | None,
    ) -> _AnalyzedCommit:
        """Проверить планы и построить новые снимки и дополнения истории."""

        candidate: dict[str, dict[str, dict[str, Any]]] = {
            kind: {} for kind in REGISTRY_FILES
        }

        blob_specs: dict[str, _BlobSpec] = {}
        update_reasons = {
            "works": {},
            "artifacts": {},
        }

        for plan in plans:
            normalized = self._normalize_plan(plan)

            for kind in (*SNAPSHOT_REGISTRY_FILES, *PLAN_HISTORY_FIELDS):
                for record_id, record in normalized[kind].items():
                    previous = candidate[kind].get(record_id)
                    records_match = (
                        previous == record
                        if kind in HISTORY_REGISTRY_FILES
                        else self._records_equivalent(kind, previous, record)
                    ) if previous is not None else True

                    if previous is not None and not records_match:
                        raise ManifestConflictError(
                            f"{kind}: пакет содержит разные записи "
                            f"с ID {record_id!r}"
                        )

                    candidate[kind].setdefault(record_id, record)

            self._merge_update_reasons(
                update_reasons["works"],
                plan.work_update_reasons,
                kind="works",
            )

            self._merge_update_reasons(
                update_reasons["artifacts"],
                plan.artifact_update_reasons,
                kind="artifacts",
            )

            for blob in plan.blobs:
                actual_sha = sha256_bytes(blob.data)

                if actual_sha != blob.sha256:
                    raise ManifestError(
                        f"blob {blob.relative_path}: заявлен SHA-256 "
                        f"{blob.sha256}, "
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

        inserted = {kind: 0 for kind in REGISTRY_FILES}
        updated = {kind: 0 for kind in REGISTRY_FILES}
        unchanged = {kind: 0 for kind in REGISTRY_FILES}
        snapshots = {
            kind: dict(current[kind]) for kind in SNAPSHOT_REGISTRY_FILES
        }

        prospective_updates = {
            kind: sum(
                1
                for record_id, record in candidate[kind].items()
                if record_id in current[kind]
                and not self._records_equivalent(
                    kind,
                    current[kind][record_id],
                    record,
                )
            )
            for kind in SNAPSHOT_REGISTRY_FILES
        }
        self._validate_expected_snapshot_hashes(
            current,
            updated=prospective_updates,
            expected=expected_snapshot_hashes,
        )

        for kind in SNAPSHOT_REGISTRY_FILES:
            for record_id, record in candidate[kind].items():
                existing = current[kind].get(record_id)

                if existing is None:
                    snapshots[kind][record_id] = record
                    inserted[kind] += 1

                elif self._records_equivalent(kind, existing, record):
                    unchanged[kind] += 1

                else:
                    self._validate_snapshot_update(
                        kind,
                        existing,
                        record,
                        reason=update_reasons[kind].get(record_id),
                        identity_conflicts={
                            **current["identity_conflicts"],
                            **candidate["identity_conflicts"],
                        },
                        work_aliases={
                            **current["work_aliases"],
                            **candidate["work_aliases"],
                        },
                    )

                    snapshots[kind][record_id] = record
                    updated[kind] += 1

        history_additions = {
            kind: [] for kind in HISTORY_REGISTRY_FILES
        }

        for kind in PLAN_HISTORY_FIELDS:
            self._collect_history_additions(
                kind,
                candidate[kind],
                current[kind],
                history_additions[kind],
                inserted,
                unchanged,
            )

        rights = {
            **current["rights"],
            **candidate["rights"],
        }
        condition_fulfilments = {
            **current["condition_fulfilments"],
            **candidate["condition_fulfilments"],
        }

        self._synthesize_legacy_condition_fulfilments(
            rights,
            condition_fulfilments,
            history_additions["condition_fulfilments"],
            inserted,
        )

        retrieval_events = {
            **current["retrieval_events"],
            **candidate["retrieval_events"],
        }

        self._synthesize_inline_retrieval_events(
            snapshots["artifacts"],
            retrieval_events,
            history_additions["retrieval_events"],
            inserted,
            allow_unresolved_rights=allow_unresolved_rights,
        )

        revision_candidates = self._build_revision_records(
            current=current,
            snapshots=snapshots,
            update_reasons=update_reasons,
            retrieval_events=retrieval_events,
        )

        for kind, records in revision_candidates.items():
            self._collect_history_additions(
                kind,
                records,
                current[kind],
                history_additions[kind],
                inserted,
                unchanged,
            )

        operation_decisions = {
            **current["operation_decisions"],
            **candidate["operation_decisions"],
        }

        self._build_operation_decisions(
            current=current,
            snapshots=snapshots,
            rights=rights,
            retrieval_events=retrieval_events,
            condition_fulfilments=condition_fulfilments,
            operation_decisions=operation_decisions,
            additions=history_additions["operation_decisions"],
            inserted=inserted,
            allow_unresolved_rights=allow_unresolved_rights,
        )

        combined: dict[str, dict[str, dict[str, Any]]] = {
            **snapshots,
            **{
                kind: {
                    **current[kind],
                    **{
                        record[PRIMARY_FIELDS[kind]]: record
                        for record in history_additions[kind]
                    },
                }
                for kind in HISTORY_REGISTRY_FILES
            },
        }

        self._validate_relations(
            combined,
            allow_unresolved_rights=allow_unresolved_rights,
        )

        self._validate_parent_cycles(combined["artifacts"])

        blob_state = self._validate_artifact_payloads(
            combined["artifacts"],
            blob_specs,
            combined["retrieval_events"],
        )

        return _AnalyzedCommit(
            snapshots=snapshots,
            history_additions=history_additions,
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            blob_state=blob_state,
        )

    @staticmethod
    def _hash_analyzed_snapshots(
        snapshots: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, str]:
        """Вычислить хеши уже проверенных снимков без повторного чтения."""

        return {
            kind: sha256_bytes(_snapshot_bytes(kind, records))
            for kind, records in snapshots.items()
        }

    @staticmethod
    def _merge_update_reasons(
        target: dict[str, str],
        additions: dict[str, str],
        *,
        kind: str,
    ) -> None:
        """Объединить причины обновлений и отклонить противоречащие значения."""

        for record_id, reason in additions.items():
            normalized_reason = reason.strip()

            if not normalized_reason:
                raise ManifestError(
                    f"{kind}: причина обновления {record_id!r} не может быть пустой"
                )

            previous = target.get(record_id)

            if previous is not None and previous != normalized_reason:
                raise ManifestConflictError(
                    f"{kind}: для {record_id!r} переданы разные причины обновления"
                )

            target[record_id] = normalized_reason

    @staticmethod
    def _validate_snapshot_update(
        kind: str,
        previous: dict[str, Any],
        current: dict[str, Any],
        *,
        reason: str | None,
        identity_conflicts: dict[str, dict[str, Any]],
        work_aliases: dict[str, dict[str, Any]],
    ) -> None:
        """Проверить обязательные инварианты обновляемой текущей записи."""

        record_id = previous[PRIMARY_FIELDS[kind]]

        if not reason or not reason.strip():
            raise ManifestConflictError(
                f"{kind}: обновление {record_id!r} требует непустую причину"
            )

        if previous["created_at"] != current["created_at"]:
            raise ManifestConflictError(
                f"{kind}: created_at записи {record_id!r} неизменяем"
            )

        previous_updated = datetime.fromisoformat(
            previous["updated_at"].replace("Z", "+00:00")
        )

        current_updated = datetime.fromisoformat(
            current["updated_at"].replace("Z", "+00:00")
        )

        if current_updated <= previous_updated:
            raise ManifestConflictError(
                f"{kind}: updated_at записи {record_id!r} должен увеличиваться"
            )

        if kind == "artifacts":
            immutable_fields = ("work_id", "artifact_id", "path", "sha256", "bytes")

            for field in immutable_fields:
                previous_value = previous.get(field)
                current_value = current.get(field)

                # Плановую запись можно материализовать один раз. После
                # появления байтов их идентичность уже не меняется.
                if previous_value is None and current_value is not None:
                    continue

                if previous_value != current_value:
                    raise ManifestConflictError(
                        f"artifacts: поле {field!r} записи {record_id!r} "
                        "неизменяемо; для других байтов нужен новый артефакт"
                    )

        if kind != "works":
            return

        conflict_fields = (
            "doi",
            "edn",
            "title",
            "published_at",
            "journal_id",
            "journal_title",
            "canonical_url",
        )

        for field in conflict_fields:
            previous_value = previous.get(field)
            current_value = current.get(field)

            if not previous_value or not current_value or previous_value == current_value:
                continue

            has_resolution = any(
                conflict["work_id"] == record_id
                and conflict["field"] == field
                and conflict["existing_value"] == str(previous_value)
                and conflict["candidate_value"] == str(current_value)
                and conflict["status"] == "resolved_replace_current"
                for conflict in identity_conflicts.values()
            )

            if not has_resolution:
                raise ManifestConflictError(
                    f"works: непустое поле {field!r} работы {record_id!r} "
                    "можно заменить только после решения конфликта"
                )

        late_alias_fields = {
            "doi": "doi",
            "edn": "edn",
            "canonical_url": "canonical_url",
        }

        for field, alias_type in late_alias_fields.items():
            previous_value = previous.get(field)
            current_value = current.get(field)

            if previous_value is not None or current_value is None:
                continue

            normalized_value = (
                normalize_doi(current_value)
                if field == "doi"
                else canonicalize_url(current_value)
                if field == "canonical_url"
                else current_value
            )
            has_verified_alias = any(
                alias["work_id"] == record_id
                and alias["alias_type"] == alias_type
                and alias["alias_value"] == normalized_value
                for alias in work_aliases.values()
            )

            if not has_verified_alias:
                raise ManifestConflictError(
                    f"works: позднее поле {field!r} работы {record_id!r} "
                    "требует проверенную запись work_aliases"
                )

    @staticmethod
    def _validate_expected_snapshot_hashes(
        current: dict[str, dict[str, dict[str, Any]]],
        *,
        updated: dict[str, int],
        expected: dict[str, str] | None,
    ) -> None:
        """Сверить optimistic-lock-хеши до формирования файлов результата."""

        supplied = expected or {}

        for kind in supplied:
            if kind not in SNAPSHOT_REGISTRY_FILES:
                raise KeyError(f"Реестр {kind!r} не является текущим снимком")

        for kind in SNAPSHOT_REGISTRY_FILES:
            if updated[kind] and kind not in supplied:
                raise ManifestConflictError(
                    f"{kind}: для обновления нужен expected_snapshot_hashes[{kind!r}]"
                )

            if kind not in supplied:
                continue

            actual = sha256_bytes(_snapshot_bytes(kind, current[kind]))

            if supplied[kind] != actual:
                raise ManifestConcurrencyError(
                    f"{kind}: ожидаемый SHA-256 прежнего снимка не совпадает "
                    f"(ожидался {supplied[kind]}, получен {actual})"
                )

    def _collect_history_additions(
        self,
        kind: str,
        candidates: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        additions: list[dict[str, Any]],
        inserted: dict[str, int],
        unchanged: dict[str, int],
    ) -> None:
        """Отделить новые строки неизменяемой истории от точных повторов."""

        primary = PRIMARY_FIELDS[kind]

        for record_id, record in candidates.items():
            self.schemas.validate(kind, record)
            existing = current.get(record_id)

            if existing is None:
                additions.append(record)
                inserted[kind] += 1
                continue

            if existing == record:
                unchanged[kind] += 1
                continue

            raise ManifestConflictError(
                f"{kind}: {primary}={record_id!r} уже существует "
                "с другим содержимым"
            )

    def _synthesize_inline_retrieval_events(
        self,
        artifacts: dict[str, dict[str, Any]],
        retrieval_events: dict[str, dict[str, Any]],
        additions: list[dict[str, Any]],
        inserted: dict[str, int],
        *,
        allow_unresolved_rights: bool,
    ) -> None:
        """Создать события metadata_only для старых встроенных retrievals."""

        for artifact in artifacts.values():
            seen_in_artifact: set[str] = set()

            for retrieval in artifact.get("retrievals", []):
                retrieval_id = retrieval["retrieval_id"]

                if retrieval_id in seen_in_artifact:
                    raise ManifestConflictError(
                        f"artifacts: {artifact['artifact_record_id']} содержит "
                        f"повторный retrieval_id {retrieval_id!r}"
                    )

                seen_in_artifact.add(retrieval_id)
                existing = retrieval_events.get(retrieval_id)

                if existing is not None:
                    self._validate_retrieval_projection(artifact, retrieval, existing)
                    continue

                if allow_unresolved_rights and not artifact.get("rights_record_ids"):
                    continue

                if not artifact.get("rights_record_ids"):
                    raise ManifestError(
                        f"artifacts: {artifact['artifact_record_id']} не имеет "
                        "записей прав для события получения"
                    )

                event = {
                    "schema_version": "retrieval-events-v1",
                    "retrieval_id": retrieval_id,
                    "created_at": retrieval["retrieved_at"],
                    "request_context_type": "artifact",
                    "request_context_id": artifact["artifact_record_id"],
                    "source_group_id": None,
                    "requested_url": retrieval["retrieved_url"],
                    "final_url": None,
                    "retrieved_at": retrieval["retrieved_at"],
                    "acquisition_method": artifact.get("acquisition_method") or "other",
                    "acquisition_scope": artifact.get("acquisition_scope") or "single",
                    "rights_record_ids": artifact.get("rights_record_ids", []),
                    # Старый формат не сохранял статус и финальный URL.
                    "http_status": None,
                    "response_headers": {},
                    "response_metadata_sha256": retrieval[
                        "response_metadata_sha256"
                    ],
                    "response_path": None,
                    "response_sha256": None,
                    "response_bytes": None,
                    "outcome": "metadata_only",
                    "error_code": None,
                    "error_detail": None,
                }

                self.schemas.validate("retrieval_events", event)
                retrieval_events[retrieval_id] = event
                additions.append(event)
                inserted["retrieval_events"] += 1

    def _synthesize_legacy_condition_fulfilments(
        self,
        rights: dict[str, dict[str, Any]],
        fulfilments: dict[str, dict[str, Any]],
        additions: list[dict[str, Any]],
        inserted: dict[str, int],
    ) -> None:
        """Перенести старые materialized-поля условий в отдельную историю."""

        for right in rights.values():
            satisfied_at = right.get("conditions_satisfied_at")
            evidence_sha256 = right.get("conditions_evidence_sha256")

            if not satisfied_at or not evidence_sha256:
                continue

            subject_type, subject_id = self._legacy_condition_subject(right)

            for condition in right["rights_conditions"]:
                already_recorded = any(
                    item["rights_record_id"] == right["rights_record_id"]
                    and item["condition"] == condition
                    and item["subject_type"] == subject_type
                    and item["subject_id"] == subject_id
                    and item["status"] == "satisfied"
                    for item in fulfilments.values()
                )

                if already_recorded:
                    continue

                expires_at = right.get("rights_expires_at")

                if expires_at:
                    expires_at = f"{expires_at}T23:59:59+00:00"

                payload = {
                    "schema_version": "condition-fulfilments-v1",
                    "created_at": satisfied_at,
                    "rights_record_id": right["rights_record_id"],
                    "condition": condition,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "status": "satisfied",
                    "satisfied_at": satisfied_at,
                    "expires_at": expires_at,
                    "evidence_sha256": evidence_sha256,
                    "supersedes_fulfilment_id": None,
                }
                digest = sha256_bytes(canonical_json(payload).encode("utf-8"))
                payload["fulfilment_id"] = f"condition-fulfilment:{digest}"
                self.schemas.validate("condition_fulfilments", payload)
                fulfilments[payload["fulfilment_id"]] = payload
                additions.append(payload)
                inserted["condition_fulfilments"] += 1

    @staticmethod
    def _legacy_condition_subject(right: dict[str, Any]) -> tuple[str, str]:
        """Выбрать допустимый субъект для переноса старого выполнения условия."""

        scope_type = right["scope_type"]

        if scope_type in {"work", "artifact", "source"}:
            return scope_type, right["scope_id"]

        return "project", PROJECT_SUBJECT_ID

    @staticmethod
    def _active_condition_fulfilments(
        fulfilments: dict[str, dict[str, Any]],
        *,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Выбрать действующие и неотозванные выполнения условий."""

        reference_time = at or datetime.now(timezone.utc)
        superseded = {
            item["supersedes_fulfilment_id"]
            for item in fulfilments.values()
            if item.get("supersedes_fulfilment_id")
            and datetime.fromisoformat(
                item["created_at"].replace("Z", "+00:00")
            ) <= reference_time
        }
        active: list[dict[str, Any]] = []

        for fulfilment_id, fulfilment in fulfilments.items():
            if fulfilment_id in superseded or fulfilment["status"] != "satisfied":
                continue

            created_at = datetime.fromisoformat(
                fulfilment["created_at"].replace("Z", "+00:00")
            )

            if created_at > reference_time:
                continue

            satisfied_at = datetime.fromisoformat(
                fulfilment["satisfied_at"].replace("Z", "+00:00")
            )

            if satisfied_at > reference_time:
                continue

            expires_at = fulfilment.get("expires_at")

            if expires_at:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

                if expires < reference_time:
                    continue

            active.append(fulfilment)

        return active

    @staticmethod
    def _condition_subject_applies(
        fulfilment: dict[str, Any],
        work: dict[str, Any],
        artifact: dict[str, Any],
    ) -> bool:
        """Проверить применимость выполнения условия к одному артефакту."""

        subject_type = fulfilment["subject_type"]
        subject_id = fulfilment["subject_id"]

        if subject_type == "project":
            return subject_id == PROJECT_SUBJECT_ID

        if subject_type == "source":
            return subject_id in {work["source_id"], work["source_group_id"]}

        if subject_type == "work":
            return subject_id in {work["work_id"], *work.get("work_aliases", [])}

        if subject_type == "artifact":
            return subject_id in {
                artifact["artifact_record_id"],
                artifact.get("artifact_id"),
            }

        return subject_id in {
            item["retrieval_id"] for item in artifact.get("retrievals", [])
        }

    @staticmethod
    def _event_context(
        event: dict[str, Any],
        works: dict[str, dict[str, Any]],
        artifacts: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Найти работу и артефакт, задающие контекст события получения."""

        context_type = event["request_context_type"]
        context_id = event["request_context_id"]

        if context_type == "artifact":
            artifact = artifacts.get(context_id)
            work = works.get(artifact["work_id"]) if artifact else None
            return work, artifact

        if context_type == "work":
            return works.get(context_id), None

        return None, None

    @classmethod
    def _right_applies_to_event(
        cls,
        right: dict[str, Any],
        event: dict[str, Any],
        work: dict[str, Any] | None,
        artifact: dict[str, Any] | None,
        *,
        source_group_ids: set[str] | None = None,
    ) -> bool:
        """Проверить область права для конкретного события получения."""

        if artifact is not None and work is not None:
            return cls._rights_apply(right, work, artifact)

        if work is not None:
            scope_type = right["scope_type"]
            scope_id = right["scope_id"]
            expected = {
                "source_group": work["source_group_id"],
                "source": work["source_id"],
                "journal": work["journal_id"],
                "work": work["work_id"],
            }

            if scope_type == "work":
                return scope_id in {work["work_id"], *work.get("work_aliases", [])}

            if scope_type == "artifact":
                return False

            return expected[scope_type] == scope_id

        if event["request_context_type"] != "source":
            return False

        if right["scope_type"] == "source":
            return right["scope_id"] == event["request_context_id"]

        return (
            right["scope_type"] == "source_group"
            and right["scope_id"] in (source_group_ids or set())
        )

    @staticmethod
    def _event_source_group_ids(
        event: dict[str, Any],
    ) -> set[str]:
        """Вернуть явно записанную группу события уровня источника."""

        if event["request_context_type"] != "source":
            return set()

        return {event["source_group_id"]}

    @staticmethod
    def _condition_subject_applies_to_event(
        fulfilment: dict[str, Any],
        event: dict[str, Any],
        work: dict[str, Any] | None,
        artifact: dict[str, Any] | None,
        *,
        source_group_ids: set[str] | None = None,
    ) -> bool:
        """Проверить субъект выполнения условия для события получения."""

        subject_type = fulfilment["subject_type"]
        subject_id = fulfilment["subject_id"]

        if subject_type == "project":
            return subject_id == PROJECT_SUBJECT_ID

        if subject_type == "retrieval":
            return subject_id == event["retrieval_id"]

        if subject_type == "artifact":
            return artifact is not None and subject_id in {
                artifact["artifact_record_id"],
                artifact.get("artifact_id"),
            }

        if subject_type == "work":
            return work is not None and subject_id in {
                work["work_id"],
                *work.get("work_aliases", []),
            }

        if work is not None:
            return subject_id in {work["source_id"], work["source_group_id"]}

        return event["request_context_type"] == "source" and subject_id in {
            event["request_context_id"],
            *(source_group_ids or set()),
        }

    @classmethod
    def _right_permits_event(
        cls,
        right: dict[str, Any],
        fulfilments: dict[str, dict[str, Any]],
        event: dict[str, Any],
        work: dict[str, Any] | None,
        artifact: dict[str, Any] | None,
        *,
        at: datetime | None = None,
        source_group_ids: set[str] | None = None,
    ) -> bool:
        """Проверить разрешение права для события и его точных условий."""

        reference_time = at or datetime.now(timezone.utc)
        expires_at = right.get("rights_expires_at")

        if expires_at and date.fromisoformat(expires_at) < reference_time.date():
            return False

        if right["status"] == "allowed":
            return True

        if right["status"] != "conditional":
            return False

        active = cls._active_condition_fulfilments(fulfilments, at=reference_time)

        return all(
            any(
                item["rights_record_id"] == right["rights_record_id"]
                and item["condition"] == condition
                and cls._condition_subject_applies_to_event(
                    item,
                    event,
                    work,
                    artifact,
                    source_group_ids=source_group_ids,
                )
                for item in active
            )
            for condition in right["rights_conditions"]
        )

    @classmethod
    def _conditions_satisfied_by_history(
        cls,
        right: dict[str, Any],
        fulfilments: dict[str, dict[str, Any]],
        work: dict[str, Any],
        artifact: dict[str, Any],
        *,
        at: datetime | None = None,
    ) -> bool:
        """Проверить каждое точное условие по отдельному журналу."""

        active = cls._active_condition_fulfilments(fulfilments, at=at)

        return all(
            any(
                item["rights_record_id"] == right["rights_record_id"]
                and item["condition"] == condition
                and cls._condition_subject_applies(item, work, artifact)
                for item in active
            )
            for condition in right["rights_conditions"]
        )

    def _build_operation_decisions(
        self,
        *,
        current: dict[str, dict[str, dict[str, Any]]],
        snapshots: dict[str, dict[str, dict[str, Any]]],
        rights: dict[str, dict[str, Any]],
        retrieval_events: dict[str, dict[str, Any]],
        condition_fulfilments: dict[str, dict[str, Any]],
        operation_decisions: dict[str, dict[str, Any]],
        additions: list[dict[str, Any]],
        inserted: dict[str, int],
        allow_unresolved_rights: bool,
    ) -> None:
        """Зафиксировать решения acquisition и storage для фактических операций."""

        superseded_rights: set[str] = set()
        rights_errors: list[str] = []
        superseded_rights = self._validate_rights_supersedes(rights, rights_errors)

        if rights_errors:
            return

        superseded_decisions = {
            item["supersedes_decision_id"]
            for item in operation_decisions.values()
            if item.get("supersedes_decision_id")
        }

        for artifact_id, artifact in snapshots["artifacts"].items():
            previous_artifact = current["artifacts"].get(artifact_id)
            changed = previous_artifact is None or not self._records_equivalent(
                "artifacts",
                previous_artifact,
                artifact,
            )
            work = snapshots["works"].get(artifact["work_id"])

            if work is None:
                continue

            operations: list[str] = []

            acquisition_status = artifact["acquisition_status"]
            acquisition_was_recorded = (
                previous_artifact is not None
                and previous_artifact["acquisition_status"] == "retrieved"
                and acquisition_status == "retrieved"
            )

            if (
                acquisition_status in {"rights_blocked", "ready", "retrieved"}
                and not acquisition_was_recorded
            ):
                operations.append("acquisition")

            if artifact.get("path") is not None:
                operations.append("storage")

            for operation in operations:
                if (
                    allow_unresolved_rights
                    and operation == "acquisition"
                    and acquisition_status == "retrieved"
                    and not artifact["rights_record_ids"]
                ):
                    continue

                decision_at = self._artifact_operation_time(
                    artifact,
                    operation=operation,
                    retrieval_events=retrieval_events,
                )

                if operation == "acquisition" and acquisition_status == "retrieved":
                    available_rights = self._rights_active_at(rights, decision_at)

                else:
                    available_rights = [
                        right
                        for right in rights.values()
                        if right["rights_record_id"] not in superseded_rights
                    ]

                applicable = [
                    right
                    for right in available_rights
                    if self._rights_apply(right, work, artifact)
                ]
                controlling = self._operation_decisions(
                    applicable,
                    operation=operation,
                    artifact=artifact,
                )

                if not controlling:
                    continue

                context = {
                    "acquisition_method": (
                        artifact["acquisition_method"]
                        if operation == "acquisition"
                        else None
                    ),
                    "acquisition_scope": (
                        artifact["acquisition_scope"]
                        if operation == "acquisition"
                        else None
                    ),
                }
                context_sha256 = sha256_bytes(
                    canonical_json(context).encode("utf-8")
                )
                decision_key = (
                    f"artifact:{artifact_id}:{operation}:"
                    f"none:{context_sha256}"
                )
                active_previous = [
                    item
                    for decision_id, item in operation_decisions.items()
                    if item["decision_key"] == decision_key
                    and decision_id not in superseded_decisions
                ]

                if active_previous and not changed:
                    continue

                selected_rights = {
                    item["rights_record_id"]: item for item in controlling
                }
                active_fulfilments = [
                    item
                    for item in self._active_condition_fulfilments(
                        condition_fulfilments,
                        at=decision_at,
                    )
                    if item["rights_record_id"] in selected_rights
                    and self._condition_subject_applies(item, work, artifact)
                ]
                permitted = all(
                    self._right_permits_at(
                        right,
                        condition_fulfilments,
                        work,
                        artifact,
                        at=decision_at,
                    )
                    for right in controlling
                )

                if permitted:
                    status = "allowed"

                elif any(right["status"] == "conditional" for right in controlling):
                    status = "conditional_pending"

                else:
                    status = "blocked"

                payload = {
                    "schema_version": "operation-decisions-v1",
                    "created_at": artifact["updated_at"],
                    "decision_key": decision_key,
                    "operation": operation,
                    "derivative_scope": None,
                    "subject_type": "artifact",
                    "subject_id": artifact_id,
                    "decision_at": decision_at.isoformat(),
                    "context": context,
                    "context_sha256": context_sha256,
                    "rights_record_ids": sorted(selected_rights),
                    "rights_snapshot_sha256": sha256_bytes(
                        _history_bytes(selected_rights)
                    ),
                    "condition_fulfilment_ids": sorted(
                        item["fulfilment_id"] for item in active_fulfilments
                    ),
                    "supersedes_decision_id": (
                        max(
                            active_previous,
                            key=lambda item: (
                                item["decision_at"],
                                item["decision_id"],
                            ),
                        )["decision_id"]
                        if active_previous
                        else None
                    ),
                    "status": status,
                }
                digest = sha256_bytes(canonical_json(payload).encode("utf-8"))
                payload["decision_id"] = f"operation-decision:{digest}"
                self.schemas.validate("operation_decisions", payload)
                existing = operation_decisions.get(payload["decision_id"])

                if existing is not None:
                    if existing != payload:
                        raise ManifestConflictError(
                            "operation_decisions: детерминированный ID занят "
                            "другим решением"
                        )

                    continue

                operation_decisions[payload["decision_id"]] = payload
                additions.append(payload)
                inserted["operation_decisions"] += 1

        for retrieval_id, event in retrieval_events.items():
            work, artifact = self._event_context(
                event,
                snapshots["works"],
                snapshots["artifacts"],
            )
            source_group_ids = self._event_source_group_ids(
                event,
            )
            retrieved_at = datetime.fromisoformat(
                event["retrieved_at"].replace("Z", "+00:00")
            )
            operations = ["acquisition"]

            if event.get("response_path") is not None:
                operations.append("storage")

            applicable = [
                right
                for right in self._rights_active_at(rights, retrieved_at)
                if self._right_applies_to_event(
                    right,
                    event,
                    work,
                    artifact,
                    source_group_ids=source_group_ids,
                )
            ]

            for operation in operations:
                controlling = self._event_operation_rights(
                    applicable,
                    operation=operation,
                    event=event,
                )

                if not controlling:
                    continue

                context = {
                    "acquisition_method": (
                        event["acquisition_method"]
                        if operation == "acquisition"
                        else None
                    ),
                    "acquisition_scope": (
                        event["acquisition_scope"]
                        if operation == "acquisition"
                        else None
                    ),
                }
                context_sha256 = sha256_bytes(
                    canonical_json(context).encode("utf-8")
                )
                decision_key = (
                    f"retrieval:{retrieval_id}:{operation}:"
                    f"none:{context_sha256}"
                )
                active_previous = [
                    item
                    for decision_id, item in operation_decisions.items()
                    if item["decision_key"] == decision_key
                    and decision_id not in superseded_decisions
                ]

                if active_previous:
                    continue

                selected_rights = {
                    item["rights_record_id"]: item for item in controlling
                }
                decision_at = retrieved_at
                active_fulfilments = [
                    item
                    for item in self._active_condition_fulfilments(
                        condition_fulfilments,
                        at=decision_at,
                    )
                    if item["rights_record_id"] in selected_rights
                    and self._condition_subject_applies_to_event(
                        item,
                        event,
                        work,
                        artifact,
                        source_group_ids=source_group_ids,
                    )
                ]
                permitted = all(
                    self._right_permits_event(
                        right,
                        condition_fulfilments,
                        event,
                        work,
                        artifact,
                        at=decision_at,
                        source_group_ids=source_group_ids,
                    )
                    for right in controlling
                )

                if permitted:
                    status = "allowed"

                elif any(right["status"] == "conditional" for right in controlling):
                    status = "conditional_pending"

                else:
                    status = "blocked"

                payload = {
                    "schema_version": "operation-decisions-v1",
                    "created_at": event["created_at"],
                    "decision_key": decision_key,
                    "operation": operation,
                    "derivative_scope": None,
                    "subject_type": "retrieval",
                    "subject_id": retrieval_id,
                    "decision_at": event["retrieved_at"],
                    "context": context,
                    "context_sha256": context_sha256,
                    "rights_record_ids": sorted(selected_rights),
                    "rights_snapshot_sha256": sha256_bytes(
                        _history_bytes(selected_rights)
                    ),
                    "condition_fulfilment_ids": sorted(
                        item["fulfilment_id"] for item in active_fulfilments
                    ),
                    "supersedes_decision_id": None,
                    "status": status,
                }
                digest = sha256_bytes(canonical_json(payload).encode("utf-8"))
                payload["decision_id"] = f"operation-decision:{digest}"
                self.schemas.validate("operation_decisions", payload)
                existing = operation_decisions.get(payload["decision_id"])

                if existing is not None:
                    if existing != payload:
                        raise ManifestConflictError(
                            "operation_decisions: детерминированный ID занят "
                            "другим решением"
                        )

                    continue

                operation_decisions[payload["decision_id"]] = payload
                additions.append(payload)
                inserted["operation_decisions"] += 1

    @staticmethod
    def _artifact_operation_time(
        artifact: dict[str, Any],
        *,
        operation: str,
        retrieval_events: dict[str, dict[str, Any]],
    ) -> datetime:
        """Вернуть момент фактической операции над артефактом."""

        if operation == "acquisition" and artifact["acquisition_status"] == "retrieved":
            retrieval_times = [
                datetime.fromisoformat(
                    retrieval["retrieved_at"].replace("Z", "+00:00")
                )
                for retrieval in artifact["retrievals"]
                if retrieval["retrieval_id"] in retrieval_events
                and retrieval_events[retrieval["retrieval_id"]]["outcome"]
                != "failed"
            ]

            if retrieval_times:
                return min(retrieval_times)

            raise ManifestError(
                "Артефакт со статусом retrieved не имеет состоявшегося "
                "события получения"
            )

        return datetime.fromisoformat(
            artifact["updated_at"].replace("Z", "+00:00")
        )

    @staticmethod
    def _validate_retrieval_projection(
        artifact: dict[str, Any],
        retrieval: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        """Сверить материализованную ссылку артефакта с событием получения."""

        projected = {
            "retrieval_id": event["retrieval_id"],
            "retrieved_url": event.get("final_url") or event["requested_url"],
            "retrieved_at": event["retrieved_at"],
            "response_metadata_sha256": event["response_metadata_sha256"],
        }

        if retrieval != projected:
            raise ManifestConflictError(
                f"artifacts: retrieval {retrieval['retrieval_id']!r} записи "
                f"{artifact['artifact_record_id']!r} не совпадает с журналом"
            )

    def _build_revision_records(
        self,
        *,
        current: dict[str, dict[str, dict[str, Any]]],
        snapshots: dict[str, dict[str, dict[str, Any]]],
        update_reasons: dict[str, dict[str, str]],
        retrieval_events: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Построить начальные и обновляющие ревизии текущих записей."""

        result = {
            "work_revisions": {},
            "artifact_revisions": {},
        }

        for kind in SNAPSHOT_REGISTRY_FILES:
            revision_kind = REVISION_KIND_BY_SNAPSHOT[kind]
            entity_field = REVISION_ENTITY_FIELD[kind]
            existing_entity_ids = {
                revision[entity_field]
                for revision in current[revision_kind].values()
            }

            for record_id, record in snapshots[kind].items():
                previous = current[kind].get(record_id)

                if kind == "works":
                    source_retrieval_ids = sorted(
                        retrieval_id
                        for retrieval_id, event in retrieval_events.items()
                        if event["request_context_type"] == "work"
                        and event["request_context_id"] == record_id
                    )

                else:
                    source_retrieval_ids = sorted(
                        retrieval["retrieval_id"]
                        for retrieval in record.get("retrievals", [])
                        if retrieval["retrieval_id"] in retrieval_events
                    )

                if record_id not in existing_entity_ids:
                    baseline = previous or record
                    revision = self._make_revision_record(
                        kind,
                        baseline,
                        previous=None,
                        reason="Первичное создание записи.",
                        source_retrieval_ids=source_retrieval_ids,
                    )

                    result[revision_kind][
                        revision[PRIMARY_FIELDS[revision_kind]]
                    ] = revision

                if previous is None or self._records_equivalent(kind, previous, record):
                    continue

                revision = self._make_revision_record(
                    kind,
                    record,
                    previous=previous,
                    reason=update_reasons[kind][record_id],
                    source_retrieval_ids=source_retrieval_ids,
                )

                result[revision_kind][
                    revision[PRIMARY_FIELDS[revision_kind]]
                ] = revision

        return result

    def _make_revision_record(
        self,
        kind: str,
        record: dict[str, Any],
        *,
        previous: dict[str, Any] | None,
        reason: str,
        source_retrieval_ids: list[str],
    ) -> dict[str, Any]:
        """Создать детерминированную строку ревизии одного снимка."""

        revision_kind = REVISION_KIND_BY_SNAPSHOT[kind]
        entity_field = REVISION_ENTITY_FIELD[kind]
        entity_id = record[PRIMARY_FIELDS[kind]]
        changed_fields = sorted(
            key
            for key, value in record.items()
            if key not in {"created_at", "updated_at"}
            and (previous is None or previous.get(key) != value)
        )

        payload = {
            "schema_version": f"{revision_kind.replace('_', '-')}-v1",
            entity_field: entity_id,
            "changed_at": (
                record["updated_at"] if previous is not None else record["created_at"]
            ),
            "reason": reason,
            "previous_sha256": (
                _record_sha256(previous) if previous is not None else None
            ),
            "new_sha256": _record_sha256(record),
            "changed_fields": changed_fields,
            "source_retrieval_ids": source_retrieval_ids,
        }

        digest = sha256_bytes(canonical_json(payload).encode("utf-8"))
        payload[PRIMARY_FIELDS[revision_kind]] = f"{revision_kind}:{digest}"
        self.schemas.validate(revision_kind, payload)

        return payload

    def audit(self) -> AuditReport:
        """Проверить схемы, внешние ключи, хеши, пути и циклы родителей."""

        errors: list[str] = []
        warnings: list[str] = []
        current: dict[str, dict[str, dict[str, Any]]] = {}

        for kind in REGISTRY_FILES:
            try:
                current[kind] = self._read_kind(kind)

            except (ManifestError, SchemaValidationError) as exception:
                errors.append(str(exception))
                current[kind] = {}

        try:
            self._validate_relations(current)

        except ManifestError as exception:
            errors.extend(str(exception).splitlines())

        try:
            self._validate_artifact_payloads(
                current["artifacts"],
                {},
                current["retrieval_events"],
            )

        except ManifestError as exception:
            errors.extend(str(exception).splitlines())

        artifact_ids: set[str] = set()

        for record in current["artifacts"].values():
            artifact_id = record.get("artifact_id")

            if artifact_id:
                if artifact_id in artifact_ids:
                    errors.append(
                        f"artifacts: artifact_id {artifact_id!r} не уникален"
                    )

                artifact_ids.add(artifact_id)

            try:
                self._validate_timestamp_order("artifacts", record)

            except ManifestError as exception:
                errors.append(str(exception))

            if record.get("acquisition_status") == "retrieved" and not record.get(
                "rights_record_ids"
            ):
                warnings.append(
                    f"artifacts: {record['artifact_record_id']} получен, "
                    "но права ещё не привязаны"
                )

            if record.get("acquisition_status") == "retrieved" and not record.get(
                "retrievals"
            ):
                warnings.append(
                    f"artifacts: {record['artifact_record_id']} не имеет "
                    "точного события "
                    "получения; происхождение неполно"
                )

        for record in current["works"].values():
            try:
                self._validate_timestamp_order("works", record)

            except ManifestError as exception:
                errors.append(str(exception))

        for record in current["rights"].values():
            try:
                self._validate_timestamp_order("rights", record, updated_required=False)

            except ManifestError as exception:
                errors.append(str(exception))

        try:
            self._validate_parent_cycles(current["artifacts"])

        except ManifestError as exception:
            errors.append(str(exception))

        if not any(current.values()):
            errors.append(
                "Рабочие реестры works.jsonl, artifacts.jsonl и "
                "rights.jsonl отсутствуют или пусты"
            )

        return AuditReport(
            counts={kind: len(records) for kind, records in current.items()},
            errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _normalize_plan(
        self, plan: ManifestPlan
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Проверить записи плана и построить индексы по первичным ключам."""

        result: dict[str, dict[str, dict[str, Any]]] = {}

        for kind in (*SNAPSHOT_REGISTRY_FILES, *PLAN_HISTORY_FIELDS):
            primary = PRIMARY_FIELDS[kind]
            index: dict[str, dict[str, Any]] = {}

            for record in getattr(plan, kind):
                self.schemas.validate(kind, record)

                if kind in SNAPSHOT_REGISTRY_FILES or kind == "rights":
                    self._validate_timestamp_order(
                        kind,
                        record,
                        updated_required=kind != "rights",
                    )

                record_id = record[primary]
                previous = index.get(record_id)

                records_match = (
                    previous == record
                    if kind in HISTORY_REGISTRY_FILES
                    else self._records_equivalent(kind, previous, record)
                ) if previous is not None else True

                if previous is not None and not records_match:
                    raise ManifestConflictError(
                        f"{kind}: план содержит разные записи "
                        f"с ID {record_id!r}"
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

        if kind in HISTORY_REGISTRY_FILES:
            return left == right

        ignored = {"created_at", "updated_at"}
        left_semantic = {
            key: value for key, value in left.items() if key not in ignored
        }

        right_semantic = {
            key: value for key, value in right.items() if key not in ignored
        }

        return left_semantic == right_semantic

    def _read_kind(self, kind: str) -> dict[str, dict[str, Any]]:
        """Прочитать и строго проверить один JSONL-реестр."""

        path = self.path_for(kind)

        if not path.exists():
            return {}

        raw = path.read_bytes()

        if raw and not raw.endswith(b"\n"):
            raise ManifestError(f"{path}: отсутствует конечный перевод строки")

        try:
            text = raw.decode("utf-8")

        except UnicodeDecodeError as exception:
            raise ManifestError(
                f"{path}: файл не является корректным UTF-8: {exception}"
            ) from exception

        primary = PRIMARY_FIELDS[kind]
        index: dict[str, dict[str, Any]] = {}

        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_object_without_duplicate_keys,
                )

            except (json.JSONDecodeError, _DuplicateJsonKeyError) as exception:
                raise ManifestError(
                    f"{path}:{line_number}: некорректный JSON: {exception}"
                ) from exception

            if not isinstance(record, dict):
                raise ManifestError(
                    f"{path}:{line_number}: строка должна быть объектом"
                )

            try:
                self.schemas.validate(kind, record)

            except SchemaValidationError as exception:
                raise SchemaValidationError(
                    f"{path}:{line_number}: {exception}"
                ) from exception

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
        """Проверить связи работ, артефактов и определяющих записей прав."""

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
                    f"artifacts: {record_id} ссылается на отсутствующего "
                    f"родителя {parent_id!r}"
                )

            elif (
                parent_id
                and by_artifact_id[parent_id]["work_id"] != artifact["work_id"]
            ):
                errors.append(
                    f"artifacts: {record_id} и его родитель относятся "
                    "к разным работам"
                )

            if (
                artifact.get("extraction_version") == "legacy-import-v1"
                and artifact.get("content_role") == "full_text"
                and artifact.get("representation") in {"plain_text", "ocr_text"}
            ):
                parent = by_artifact_id.get(parent_id) if parent_id else None
                permitted_methods = {"legacy_pdf", "legacy_pdf_ocr_layout"}

                if (
                    parent is None or
                    parent.get("representation") != "pdf" or
                    artifact.get("extraction_method") not in permitted_methods
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
                        f"artifacts: {record_id} ссылается на отсутствующий "
                        "rights_record_id "
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
                event = records["retrieval_events"].get(retrieval_id)

                if event is None:
                    if allow_unresolved_rights and not artifact.get(
                        "rights_record_ids"
                    ):
                        continue

                    errors.append(
                        f"artifacts: {record_id} ссылается на отсутствующее "
                        f"событие получения {retrieval_id!r}"
                    )

                    continue

                try:
                    self._validate_retrieval_projection(artifact, retrieval, event)

                except ManifestError as exception:
                    errors.append(str(exception))

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

            if artifact["acquisition_status"] == "ready":
                self._require_permitting_right(
                    active_rights,
                    all_active_applicable_rights,
                    operation="acquisition",
                    artifact=artifact,
                    work=work,
                    fulfilments=records["condition_fulfilments"],
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
                        f"artifacts: {record_id} имеет rights_blocked "
                        "без записи acquisition"
                    )

                elif not any(
                    item["rights_record_id"] in referenced_ids for item in decisions
                ):
                    errors.append(
                        f"artifacts: {record_id} не ссылается на определяющую "
                        "запись acquisition"
                    )

                elif all(
                    self._right_permits(
                        item,
                        records["condition_fulfilments"],
                        work,
                        artifact,
                    )
                    for item in decisions
                ):
                    errors.append(
                        f"artifacts: {record_id} имеет rights_blocked, "
                        "но право acquisition "
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
                    work=work,
                    fulfilments=records["condition_fulfilments"],
                    artifact_record_id=record_id,
                    errors=errors,
                )

        self._validate_revision_histories(records, errors)
        self._validate_retrieval_event_relations(records, errors)
        self._validate_work_alias_relations(records, errors)
        self._validate_identity_conflict_relations(records, errors)
        self._validate_condition_fulfilment_relations(records, errors)
        self._validate_operation_decision_relations(
            records,
            errors,
            require_coverage=not allow_unresolved_rights,
        )

        if errors:
            raise ManifestError("\n".join(errors))

    @staticmethod
    def _validate_revision_histories(
        records: dict[str, dict[str, dict[str, Any]]],
        errors: list[str],
    ) -> None:
        """Проверить цепочки ревизий и их соответствие текущим снимкам."""

        retrieval_ids = set(records["retrieval_events"])

        for kind in SNAPSHOT_REGISTRY_FILES:
            revision_kind = REVISION_KIND_BY_SNAPSHOT[kind]
            entity_field = REVISION_ENTITY_FIELD[kind]
            by_entity: dict[str, list[dict[str, Any]]] = {}

            for revision in records[revision_kind].values():
                entity_id = revision[entity_field]
                by_entity.setdefault(entity_id, []).append(revision)

                if entity_id not in records[kind]:
                    errors.append(
                        f"{revision_kind}: ревизия ссылается на отсутствующий "
                        f"{entity_field}={entity_id!r}"
                    )

                for retrieval_id in revision["source_retrieval_ids"]:
                    if retrieval_id not in retrieval_ids:
                        errors.append(
                            f"{revision_kind}: неизвестный source_retrieval_id "
                            f"{retrieval_id!r}"
                        )

            for entity_id, revisions in by_entity.items():
                ordered = sorted(
                    revisions,
                    key=lambda item: (
                        datetime.fromisoformat(
                            item["changed_at"].replace("Z", "+00:00")
                        ),
                        item[PRIMARY_FIELDS[revision_kind]],
                    ),
                )

                if ordered[0]["previous_sha256"] is not None:
                    errors.append(
                        f"{revision_kind}: первая ревизия {entity_id!r} "
                        "должна иметь previous_sha256=null"
                    )

                previous_sha256: str | None = None

                for index, revision in enumerate(ordered):
                    if index and revision["previous_sha256"] != previous_sha256:
                        errors.append(
                            f"{revision_kind}: разрыв цепочки ревизий {entity_id!r}"
                        )

                    previous_sha256 = revision["new_sha256"]

                current_record = records[kind].get(entity_id)

                if (
                    current_record is not None
                    and previous_sha256 != _record_sha256(current_record)
                ):
                    errors.append(
                        f"{revision_kind}: последняя ревизия {entity_id!r} "
                        "не соответствует текущему снимку"
                    )

            for entity_id in records[kind]:
                if entity_id not in by_entity:
                    errors.append(
                        f"{revision_kind}: для {entity_id!r} отсутствует "
                        "начальная ревизия"
                    )

    @classmethod
    def _validate_retrieval_event_relations(
        cls,
        records: dict[str, dict[str, dict[str, Any]]],
        errors: list[str],
    ) -> None:
        """Проверить контексты, права и материализацию событий получения."""

        works = records["works"]
        artifacts = records["artifacts"]
        rights = records["rights"]
        fulfilments = records["condition_fulfilments"]

        for event in records["retrieval_events"].values():
            retrieval_id = event["retrieval_id"]
            context_type = event["request_context_type"]
            context_id = event["request_context_id"]
            context_artifact = artifacts.get(context_id)
            event_created_at = datetime.fromisoformat(
                event["created_at"].replace("Z", "+00:00")
            )
            retrieved_at = datetime.fromisoformat(
                event["retrieved_at"].replace("Z", "+00:00")
            )

            if retrieved_at > event_created_at:
                errors.append(
                    f"retrieval_events: {retrieval_id}: retrieved_at позже "
                    "created_at записи"
                )

            if event_created_at > datetime.now(timezone.utc):
                errors.append(
                    f"retrieval_events: {retrieval_id}: created_at в будущем"
                )

            if context_type == "work" and context_id not in works:
                errors.append(
                    f"retrieval_events: {retrieval_id} ссылается на "
                    f"отсутствующую работу {context_id!r}"
                )

            elif context_type == "artifact" and context_id not in artifacts:
                errors.append(
                    f"retrieval_events: {retrieval_id} ссылается на "
                    f"отсутствующий артефакт {context_id!r}"
                )

            elif context_type == "artifact" and not any(
                item["retrieval_id"] == retrieval_id
                for item in context_artifact.get("retrievals", [])
            ):
                errors.append(
                    f"retrieval_events: {retrieval_id} не материализован "
                    f"в retrievals артефакта {context_id!r}"
                )

            work, artifact = cls._event_context(event, works, artifacts)
            source_group_ids = cls._event_source_group_ids(event)
            cls._validate_event_source_group(event, works, errors)
            referenced_rights: list[dict[str, Any]] = []

            for rights_id in event["rights_record_ids"]:
                right = rights.get(rights_id)

                if right is None:
                    errors.append(
                        f"retrieval_events: {retrieval_id} ссылается на "
                        f"неизвестное право {rights_id!r}"
                    )

                elif not cls._right_applies_to_event(
                    right,
                    event,
                    work,
                    artifact,
                    source_group_ids=source_group_ids,
                ):
                    errors.append(
                        f"retrieval_events: право {rights_id!r} не применимо "
                        f"к событию {retrieval_id!r}"
                    )

                elif right["operation"] == "acquisition" and (
                    right["acquisition_method"] != event["acquisition_method"]
                    or right["acquisition_scope"] != event["acquisition_scope"]
                ):
                    errors.append(
                        f"retrieval_events: право {rights_id!r} не соответствует "
                        f"режиму события {retrieval_id!r}"
                    )

                else:
                    referenced_rights.append(right)

            rights_at_event = cls._rights_active_at(rights, retrieved_at)
            all_active_applicable_rights = [
                right
                for right in rights_at_event
                if cls._right_applies_to_event(
                    right,
                    event,
                    work,
                    artifact,
                    source_group_ids=source_group_ids,
                )
            ]
            referenced_ids = {
                right["rights_record_id"]
                for right in referenced_rights
                if right in rights_at_event
            }
            required_operations = ["acquisition"]

            if event.get("response_path") is not None:
                required_operations.append("storage")

            for operation in required_operations:
                controlling = cls._event_operation_rights(
                    all_active_applicable_rights,
                    operation=operation,
                    event=event,
                )

                if not controlling:
                    errors.append(
                        f"retrieval_events: {retrieval_id} не имеет "
                        f"применимой записи {operation}"
                    )
                    continue

                controlling_ids = {
                    right["rights_record_id"] for right in controlling
                }

                if not controlling_ids.issubset(referenced_ids):
                    errors.append(
                        f"retrieval_events: {retrieval_id} не ссылается "
                        f"на определяющие права {operation}"
                    )

                factual_operation = (
                    operation == "storage" or event["outcome"] != "failed"
                )

                if factual_operation and not all(
                    cls._right_permits_event(
                        right,
                        fulfilments,
                        event,
                        work,
                        artifact,
                        at=retrieved_at,
                        source_group_ids=source_group_ids,
                    )
                    for right in controlling
                ):
                    errors.append(
                        f"retrieval_events: {retrieval_id}: операция "
                        f"{operation} не разрешена"
                    )

            if event.get("response_path") is not None:
                cls._validate_current_retrieval_storage(
                    event,
                    work,
                    artifact,
                    rights,
                    fulfilments,
                    errors,
                    source_group_ids=source_group_ids,
                )

    @classmethod
    def _validate_current_retrieval_storage(
        cls,
        event: dict[str, Any],
        work: dict[str, Any] | None,
        artifact: dict[str, Any] | None,
        rights: dict[str, dict[str, Any]],
        fulfilments: dict[str, dict[str, Any]],
        errors: list[str],
        *,
        source_group_ids: set[str],
    ) -> None:
        """Проверить текущее право хранить сохранённый HTTP-ответ."""

        reference_time = datetime.now(timezone.utc)
        applicable = [
            right
            for right in cls._rights_active_at(rights, reference_time)
            if cls._right_applies_to_event(
                right,
                event,
                work,
                artifact,
                source_group_ids=source_group_ids,
            )
        ]
        controlling = cls._event_operation_rights(
            applicable,
            operation="storage",
            event=event,
        )
        retrieval_id = event["retrieval_id"]

        if not controlling:
            errors.append(
                f"retrieval_events: {retrieval_id} не имеет текущего "
                "права на хранение ответа"
            )
            return

        if not all(
            cls._right_permits_event(
                right,
                fulfilments,
                event,
                work,
                artifact,
                at=reference_time,
                source_group_ids=source_group_ids,
            )
            for right in controlling
        ):
            errors.append(
                f"retrieval_events: текущее хранение ответа {retrieval_id!r} "
                "не разрешено"
            )

    @staticmethod
    def _validate_event_source_group(
        event: dict[str, Any],
        works: dict[str, dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Сверить группу события с известными карточками того же источника."""

        if event["request_context_type"] != "source":
            return

        source_id = event["request_context_id"]
        known_groups = {
            work["source_group_id"]
            for work in works.values()
            if work["source_id"] == source_id
        }

        if not known_groups:
            return

        source_group_id = event["source_group_id"]

        if known_groups != {source_group_id}:
            errors.append(
                f"retrieval_events: группа {source_group_id!r} источника "
                f"{source_id!r} не совпадает с карточками работ"
            )

    @staticmethod
    def _alias_identity(alias: dict[str, Any]) -> str:
        """Преобразовать проверенный псевдоним в строку текущей карточки."""

        alias_type = alias["alias_type"]
        value = alias["alias_value"]

        if alias_type == "doi":
            normalized = normalize_doi(value)

            if normalized is None or normalized != value:
                raise ManifestError(
                    f"work_aliases: DOI {value!r} не нормализован"
                )

            return f"doi:{normalized}"

        if alias_type == "canonical_url":
            normalized_url = canonicalize_url(value)

            if normalized_url != value:
                raise ManifestError(
                    f"work_aliases: URL {value!r} не канонизирован"
                )

            return f"url:{normalized_url}"

        return f"{alias_type}:{value}"

    @classmethod
    def _validate_work_alias_relations(
        cls,
        records: dict[str, dict[str, dict[str, Any]]],
        errors: list[str],
    ) -> None:
        """Проверить владельцев, доказательства и материализацию псевдонимов."""

        works = records["works"]
        retrievals = records["retrieval_events"]
        alias_owners: dict[str, str] = {}
        successors: dict[str, str] = {}

        for alias in records["work_aliases"].values():
            work_id = alias["work_id"]
            work = works.get(work_id)

            if work is None:
                errors.append(
                    f"work_aliases: псевдоним ссылается на отсутствующий "
                    f"work_id {work_id!r}"
                )

                continue

            retrieval_id = alias.get("source_retrieval_id")

            if retrieval_id and retrieval_id not in retrievals:
                errors.append(
                    f"work_aliases: неизвестный source_retrieval_id "
                    f"{retrieval_id!r}"
                )

            try:
                identity = cls._alias_identity(alias)

            except ManifestError as exception:
                errors.append(str(exception))
                continue

            if identity not in work.get("work_aliases", []):
                errors.append(
                    f"work_aliases: {identity!r} не материализован в работе "
                    f"{work_id!r}"
                )

            previous_owner = alias_owners.get(identity)

            if previous_owner and previous_owner != work_id:
                errors.append(
                    f"work_aliases: {identity!r} принадлежит двум работам"
                )

            alias_owners[identity] = work_id
            previous_id = alias.get("supersedes_alias_record_id")

            if not previous_id:
                continue

            previous = records["work_aliases"].get(previous_id)

            if previous is None:
                errors.append(
                    f"work_aliases: {alias['alias_record_id']} заменяет "
                    f"неизвестную запись {previous_id!r}"
                )
                continue

            comparable = ("work_id", "alias_type", "alias_value")

            if any(alias[field] != previous[field] for field in comparable):
                errors.append(
                    f"work_aliases: {alias['alias_record_id']} заменяет "
                    "псевдоним другой работы или другого значения"
                )

            alias_created = datetime.fromisoformat(
                alias["created_at"].replace("Z", "+00:00")
            )
            previous_created = datetime.fromisoformat(
                previous["created_at"].replace("Z", "+00:00")
            )

            if alias_created <= previous_created:
                errors.append(
                    f"work_aliases: {alias['alias_record_id']} должен быть "
                    "создан позже заменяемой записи"
                )

            if previous_id in successors:
                errors.append(
                    f"work_aliases: {previous_id!r} имеет два преемника"
                )

            successors[previous_id] = alias["alias_record_id"]

        for work_id, work in works.items():
            normalized_doi = normalize_doi(work.get("doi"))

            if work.get("doi") is not None and normalized_doi != work["doi"]:
                errors.append(f"works: DOI работы {work_id!r} не нормализован")

            if normalized_doi is None or work_id == f"doi:{normalized_doi}":
                continue

            expected_alias = f"doi:{normalized_doi}"

            if alias_owners.get(expected_alias) != work_id:
                errors.append(
                    f"works: поздний DOI работы {work_id!r} не подтверждён "
                    "записью work_aliases"
                )

    @staticmethod
    def _validate_identity_conflict_relations(
        records: dict[str, dict[str, dict[str, Any]]],
        errors: list[str],
    ) -> None:
        """Проверить карантин и происхождение конфликтов идентичности."""

        works = records["works"]
        retrieval_ids = set(records["retrieval_events"])
        conflicts = list(records["identity_conflicts"].values())

        def conflict_key(conflict: dict[str, Any]) -> tuple[str, str, str, str]:
            """Построить ключ одного предмета ручного разрешения."""

            return (
                conflict["work_id"],
                conflict["field"],
                conflict["existing_value"],
                conflict["candidate_value"],
            )

        resolutions_by_key: dict[
            tuple[str, str, str, str],
            list[dict[str, Any]],
        ] = {}

        for conflict in conflicts:
            if conflict["status"] != "pending":
                resolutions_by_key.setdefault(conflict_key(conflict), []).append(
                    conflict
                )

        for key, resolutions in resolutions_by_key.items():
            if len(resolutions) > 1:
                errors.append(
                    "identity_conflicts: конфликт "
                    f"{key!r} имеет несколько решений"
                )

        for conflict in conflicts:
            work = works.get(conflict["work_id"])

            if work is None:
                errors.append(
                    f"identity_conflicts: неизвестный work_id "
                    f"{conflict['work_id']!r}"
                )
                continue

            for retrieval_id in conflict["source_retrieval_ids"]:
                if retrieval_id not in retrieval_ids:
                    errors.append(
                        f"identity_conflicts: неизвестный source_retrieval_id "
                        f"{retrieval_id!r}"
                    )

            field = conflict["field"]
            current_value = work.get(field)

            if conflict["status"] == "pending":
                pending_created = datetime.fromisoformat(
                    conflict["created_at"].replace("Z", "+00:00")
                )
                later_resolutions = [
                    resolution
                    for resolution in resolutions_by_key.get(
                        conflict_key(conflict),
                        [],
                    )
                    if datetime.fromisoformat(
                        resolution["created_at"].replace("Z", "+00:00")
                    ) > pending_created
                ]

                if later_resolutions:
                    continue

                if work["eligibility_status"] != "quarantined":
                    errors.append(
                        f"identity_conflicts: работа {work['work_id']!r} "
                        "с открытым конфликтом не помещена в карантин"
                    )

                if str(current_value) != conflict["existing_value"]:
                    errors.append(
                        f"identity_conflicts: прежнее значение поля {field!r} "
                        "не сохранено в текущем снимке"
                    )

            elif conflict["status"] == "resolved_keep_current":
                if str(current_value) != conflict["existing_value"]:
                    errors.append(
                        f"identity_conflicts: решение сохранить поле {field!r} "
                        "не отражено в работе"
                    )

            elif str(current_value) != conflict["candidate_value"]:
                errors.append(
                    f"identity_conflicts: решение заменить поле {field!r} "
                    "не отражено в работе"
                )

    @staticmethod
    def _validate_condition_fulfilment_relations(
        records: dict[str, dict[str, dict[str, Any]]],
        errors: list[str],
    ) -> None:
        """Проверить точные условия, субъекты и цепочки их выполнения."""

        rights = records["rights"]
        works = records["works"]
        artifacts = records["artifacts"]
        retrievals = records["retrieval_events"]
        successors: dict[str, str] = {}
        graph: dict[str, str] = {}
        now = datetime.now(timezone.utc)

        for fulfilment in records["condition_fulfilments"].values():
            fulfilment_id = fulfilment["fulfilment_id"]
            right = rights.get(fulfilment["rights_record_id"])
            satisfied_at = datetime.fromisoformat(
                fulfilment["satisfied_at"].replace("Z", "+00:00")
            )

            if satisfied_at > now:
                errors.append(
                    f"condition_fulfilments: {fulfilment_id} имеет "
                    "satisfied_at в будущем"
                )

            if right is None:
                errors.append(
                    f"condition_fulfilments: {fulfilment_id} ссылается на "
                    "неизвестную запись прав"
                )

            elif fulfilment["condition"] not in right["rights_conditions"]:
                errors.append(
                    f"condition_fulfilments: условие {fulfilment_id} "
                    "отсутствует в записи прав"
                )

            subject_type = fulfilment["subject_type"]
            subject_id = fulfilment["subject_id"]

            if subject_type == "work" and subject_id not in works:
                errors.append(
                    f"condition_fulfilments: неизвестная работа {subject_id!r}"
                )

            elif subject_type == "artifact" and subject_id not in artifacts:
                errors.append(
                    f"condition_fulfilments: неизвестный артефакт {subject_id!r}"
                )

            elif subject_type == "retrieval" and subject_id not in retrievals:
                errors.append(
                    f"condition_fulfilments: неизвестное получение {subject_id!r}"
                )

            previous_id = fulfilment.get("supersedes_fulfilment_id")

            if not previous_id:
                continue

            previous = records["condition_fulfilments"].get(previous_id)

            if previous is None:
                errors.append(
                    f"condition_fulfilments: {fulfilment_id} заменяет "
                    f"неизвестную запись {previous_id!r}"
                )
                continue

            comparable = ("rights_record_id", "condition", "subject_type", "subject_id")

            if any(fulfilment[field] != previous[field] for field in comparable):
                errors.append(
                    f"condition_fulfilments: {fulfilment_id} заменяет "
                    "выполнение другого условия или субъекта"
                )

            created_at = datetime.fromisoformat(
                fulfilment["created_at"].replace("Z", "+00:00")
            )
            previous_created_at = datetime.fromisoformat(
                previous["created_at"].replace("Z", "+00:00")
            )

            if created_at <= previous_created_at:
                errors.append(
                    f"condition_fulfilments: {fulfilment_id} должен быть "
                    "создан позже заменяемой записи"
                )

            if previous_id in successors:
                errors.append(
                    f"condition_fulfilments: {previous_id!r} имеет два преемника"
                )

            successors[previous_id] = fulfilment_id
            graph[fulfilment_id] = previous_id

        for start in graph:
            seen: set[str] = set()
            current: str | None = start

            while current in graph:
                if current in seen:
                    errors.append(
                        f"condition_fulfilments: цикл supersedes около {current!r}"
                    )
                    break

                seen.add(current)
                current = graph[current]

    @classmethod
    def _validate_operation_decision_relations(
        cls,
        records: dict[str, dict[str, dict[str, Any]]],
        errors: list[str],
        *,
        require_coverage: bool,
    ) -> None:
        """Проверить основания и цепочки исторических решений по операциям."""

        rights = records["rights"]
        fulfilments = records["condition_fulfilments"]
        decisions = records["operation_decisions"]
        successors: dict[str, str] = {}
        graph: dict[str, str] = {}

        for decision_id, decision in decisions.items():
            actual_context_sha256 = sha256_bytes(
                canonical_json(decision["context"]).encode("utf-8")
            )

            if decision["context_sha256"] != actual_context_sha256:
                errors.append(
                    f"operation_decisions: context_sha256 решения "
                    f"{decision_id!r} не совпадает"
                )

            for rights_id in decision["rights_record_ids"]:
                if rights_id not in rights:
                    errors.append(
                        f"operation_decisions: {decision_id} ссылается на "
                        f"неизвестное право {rights_id!r}"
                    )

            selected_rights = {
                rights_id: rights[rights_id]
                for rights_id in decision["rights_record_ids"]
                if rights_id in rights
            }

            if selected_rights and decision["rights_snapshot_sha256"] != sha256_bytes(
                _history_bytes(selected_rights)
            ):
                errors.append(
                    f"operation_decisions: rights_snapshot_sha256 решения "
                    f"{decision_id!r} не совпадает"
                )

            cls._validate_operation_decision_semantics(
                decision,
                records,
                selected_rights,
                errors,
            )

            for fulfilment_id in decision["condition_fulfilment_ids"]:
                if fulfilment_id not in fulfilments:
                    errors.append(
                        f"operation_decisions: {decision_id} ссылается на "
                        f"неизвестное выполнение условия {fulfilment_id!r}"
                    )

            previous_id = decision.get("supersedes_decision_id")

            if not previous_id:
                continue

            previous = decisions.get(previous_id)

            if previous is None:
                errors.append(
                    f"operation_decisions: {decision_id} заменяет "
                    f"неизвестное решение {previous_id!r}"
                )
                continue

            comparable = (
                "decision_key",
                "operation",
                "derivative_scope",
                "subject_type",
                "subject_id",
            )

            if any(decision[field] != previous[field] for field in comparable):
                errors.append(
                    f"operation_decisions: {decision_id} заменяет решение "
                    "с другим ключом, субъектом или типом производного объекта"
                )

            decision_at = datetime.fromisoformat(
                decision["decision_at"].replace("Z", "+00:00")
            )
            previous_decision_at = datetime.fromisoformat(
                previous["decision_at"].replace("Z", "+00:00")
            )

            if decision_at <= previous_decision_at:
                errors.append(
                    f"operation_decisions: {decision_id} должно быть принято "
                    "позже заменяемого решения"
                )

            if previous_id in successors:
                errors.append(
                    f"operation_decisions: {previous_id!r} имеет два преемника"
                )

            successors[previous_id] = decision_id
            graph[decision_id] = previous_id

        for start in graph:
            seen: set[str] = set()
            current: str | None = start

            while current in graph:
                if current in seen:
                    errors.append(
                        f"operation_decisions: цикл supersedes около {current!r}"
                    )
                    break

                seen.add(current)
                current = graph[current]

        superseded_decision_ids = set(successors)
        active_by_key: dict[str, str] = {}

        for decision_id, decision in decisions.items():
            if decision_id in superseded_decision_ids:
                continue

            previous_id = active_by_key.get(decision["decision_key"])

            if previous_id is not None:
                errors.append(
                    f"operation_decisions: ключ {decision['decision_key']!r} "
                    f"имеет два активных решения: {previous_id!r} и {decision_id!r}"
                )

            active_by_key[decision["decision_key"]] = decision_id

        if not require_coverage:
            return

        active_decisions = [
            decision
            for decision_id, decision in decisions.items()
            if decision_id not in superseded_decision_ids
        ]

        for artifact_id, artifact in records["artifacts"].items():
            operations: list[str] = []

            if artifact["acquisition_status"] in {
                "rights_blocked",
                "ready",
                "retrieved",
            }:
                operations.append("acquisition")

            if artifact.get("path") is not None:
                operations.append("storage")

            for operation in operations:
                matching_decisions = [
                    item
                    for item in active_decisions
                    if item["subject_type"] == "artifact"
                    and item["subject_id"] == artifact_id
                    and item["operation"] == operation
                ]

                if not matching_decisions:
                    errors.append(
                        f"operation_decisions: для артефакта {artifact_id!r} "
                        f"нет активного решения {operation}"
                    )

                elif (
                    operation == "acquisition"
                    and artifact["acquisition_status"] == "retrieved"
                    and not any(
                        item["status"] == "allowed"
                        for item in matching_decisions
                    )
                ):
                    errors.append(
                        f"operation_decisions: получение артефакта "
                        f"{artifact_id!r} не имеет разрешающего решения"
                    )

        for retrieval_id, event in records["retrieval_events"].items():
            operations = ["acquisition"]

            if event.get("response_path") is not None:
                operations.append("storage")

            for operation in operations:
                if not any(
                    item["subject_type"] == "retrieval"
                    and item["subject_id"] == retrieval_id
                    and item["operation"] == operation
                    for item in active_decisions
                ):
                    errors.append(
                        f"operation_decisions: для получения {retrieval_id!r} "
                        f"нет активного решения {operation}"
                    )

    @classmethod
    def _validate_operation_decision_semantics(
        cls,
        decision: dict[str, Any],
        records: dict[str, dict[str, dict[str, Any]]],
        selected_rights: dict[str, dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Сверить историческое решение с субъектом, правами и условиями."""

        decision_id = decision["decision_id"]
        subject_type = decision["subject_type"]
        subject_id = decision["subject_id"]
        decision_at = datetime.fromisoformat(
            decision["decision_at"].replace("Z", "+00:00")
        )
        created_at = datetime.fromisoformat(
            decision["created_at"].replace("Z", "+00:00")
        )
        operation = decision["operation"]

        if decision_at > created_at:
            errors.append(
                f"operation_decisions: {decision_id}: decision_at позже created_at"
            )

        if created_at > datetime.now(timezone.utc):
            errors.append(
                f"operation_decisions: {decision_id}: created_at в будущем"
            )

        if subject_type == "project" and subject_id != PROJECT_SUBJECT_ID:
            errors.append(
                f"operation_decisions: {decision_id} относится к чужому проекту"
            )
            return

        if subject_type == "work" and subject_id not in records["works"]:
            errors.append(
                f"operation_decisions: {decision_id} ссылается на "
                f"неизвестную работу {subject_id!r}"
            )
            return

        if subject_type == "artifact":
            artifact = records["artifacts"].get(subject_id)

            if artifact is None:
                errors.append(
                    f"operation_decisions: {decision_id} ссылается на "
                    f"неизвестный артефакт {subject_id!r}"
                )
                return

            work = records["works"].get(artifact["work_id"])

            if work is None:
                return

            expected_context = {
                "acquisition_method": (
                    artifact["acquisition_method"]
                    if operation == "acquisition"
                    else None
                ),
                "acquisition_scope": (
                    artifact["acquisition_scope"]
                    if operation == "acquisition"
                    else None
                ),
            }
            applicable = [
                right
                for right in cls._rights_active_at(records["rights"], decision_at)
                if cls._rights_apply(right, work, artifact)
                and (
                    operation != "derivatives_release"
                    or decision["derivative_scope"] in right["derivative_scope"]
                )
            ]
            controlling = cls._operation_decisions(
                applicable,
                operation=operation,
                artifact=artifact,
            )

            def artifact_permits(right: dict[str, Any]) -> bool:
                """Проверить право решения для выбранного артефакта."""

                return cls._right_permits_at(
                    right,
                    records["condition_fulfilments"],
                    work,
                    artifact,
                    at=decision_at,
                )

            def artifact_subject_applies(item: dict[str, Any]) -> bool:
                """Проверить выполнение условия для выбранного артефакта."""

                return cls._condition_subject_applies(item, work, artifact)

            permits = artifact_permits
            subject_applies = artifact_subject_applies

        elif subject_type == "retrieval":
            event = records["retrieval_events"].get(subject_id)

            if event is None:
                errors.append(
                    f"operation_decisions: {decision_id} ссылается на "
                    f"неизвестное получение {subject_id!r}"
                )
                return

            if operation == "storage" and event.get("response_path") is None:
                errors.append(
                    f"operation_decisions: {decision_id} фиксирует storage "
                    "без сохранённого ответа"
                )

            work, artifact = cls._event_context(
                event,
                records["works"],
                records["artifacts"],
            )
            source_group_ids = cls._event_source_group_ids(
                event,
            )
            expected_context = {
                "acquisition_method": (
                    event["acquisition_method"]
                    if operation == "acquisition"
                    else None
                ),
                "acquisition_scope": (
                    event["acquisition_scope"]
                    if operation == "acquisition"
                    else None
                ),
            }
            applicable = [
                right
                for right in cls._rights_active_at(records["rights"], decision_at)
                if cls._right_applies_to_event(
                    right,
                    event,
                    work,
                    artifact,
                    source_group_ids=source_group_ids,
                )
                and (
                    operation != "derivatives_release"
                    or decision["derivative_scope"] in right["derivative_scope"]
                )
            ]
            controlling = cls._event_operation_rights(
                applicable,
                operation=operation,
                event=event,
            )

            def retrieval_permits(right: dict[str, Any]) -> bool:
                """Проверить право решения для выбранного получения."""

                return cls._right_permits_event(
                    right,
                    records["condition_fulfilments"],
                    event,
                    work,
                    artifact,
                    at=decision_at,
                    source_group_ids=source_group_ids,
                )

            def retrieval_subject_applies(item: dict[str, Any]) -> bool:
                """Проверить выполнение условия для выбранного получения."""

                return cls._condition_subject_applies_to_event(
                    item,
                    event,
                    work,
                    artifact,
                    source_group_ids=source_group_ids,
                )

            permits = retrieval_permits
            subject_applies = retrieval_subject_applies

        else:
            # Для решений уровня проекта, источника и работы в текущих схемах
            # нет полного контекста артефакта. Их хеши и ссылки всё равно
            # проверяются, а строгая семантика применяется к фактам хранения.
            return

        if decision["context"] != expected_context:
            errors.append(
                f"operation_decisions: контекст решения {decision_id!r} "
                "не соответствует субъекту"
            )

        controlling_ids = {
            right["rights_record_id"] for right in controlling
        }

        if not controlling_ids:
            errors.append(
                f"operation_decisions: {decision_id} не имеет определяющих прав"
            )

        elif set(selected_rights) != controlling_ids:
            errors.append(
                f"operation_decisions: {decision_id} содержит не тот набор "
                "определяющих прав"
            )

        permitted = bool(controlling) and all(permits(right) for right in controlling)

        if permitted:
            expected_status = "allowed"

        elif any(right["status"] == "conditional" for right in controlling):
            expected_status = "conditional_pending"

        else:
            expected_status = "blocked"

        if decision["status"] != expected_status:
            errors.append(
                f"operation_decisions: статус решения {decision_id!r} "
                f"должен быть {expected_status!r}"
            )

        active_fulfilments = cls._active_condition_fulfilments(
            records["condition_fulfilments"],
            at=decision_at,
        )
        expected_fulfilment_ids = {
            item["fulfilment_id"]
            for item in active_fulfilments
            if item["rights_record_id"] in controlling_ids
            and subject_applies(item)
        }

        if set(decision["condition_fulfilment_ids"]) != expected_fulfilment_ids:
            errors.append(
                f"operation_decisions: выполнения условий решения "
                f"{decision_id!r} не соответствуют журналу"
            )

    @staticmethod
    def _rights_active_at(
        rights: dict[str, dict[str, Any]],
        at: datetime,
    ) -> list[dict[str, Any]]:
        """Выбрать версии прав, действовавшие как записи в заданный момент."""

        available = {
            rights_id: right
            for rights_id, right in rights.items()
            if datetime.fromisoformat(
                right["created_at"].replace("Z", "+00:00")
            ) <= at
        }
        superseded = {
            right["supersedes_rights_record_id"]
            for right in available.values()
            if right.get("supersedes_rights_record_id") in available
        }

        return [
            right
            for rights_id, right in available.items()
            if rights_id not in superseded
        ]

    def _require_permitting_right(
        self,
        rights: list[dict[str, Any]],
        all_applicable_rights: list[dict[str, Any]],
        *,
        operation: str,
        artifact: dict[str, Any],
        work: dict[str, Any],
        fulfilments: dict[str, dict[str, Any]],
        artifact_record_id: str,
        errors: list[str],
    ) -> None:
        """Добавить ошибку, если определяющее право не разрешает операцию."""

        decisions = self._operation_decisions(
            all_applicable_rights,
            operation=operation,
            artifact=artifact,
        )

        if not decisions:
            errors.append(
                f"artifacts: {artifact_record_id} не имеет "
                f"применимой записи {operation}"
            )

            return

        referenced_ids = {item["rights_record_id"] for item in rights}

        if not any(item["rights_record_id"] in referenced_ids for item in decisions):
            errors.append(
                f"artifacts: {artifact_record_id} не ссылается на наиболее "
                f"конкретную запись {operation}"
            )

        if not all(
            self._right_permits(item, fulfilments, work, artifact)
            for item in decisions
        ):
            details: set[str] = set()

            for item in decisions:
                detail = item["status"]

                if _right_is_expired(item):
                    detail += ":expired"

                elif (
                    item["status"] == "conditional"
                    and not self._conditions_satisfied_by_history(
                        item,
                        fulfilments,
                        work,
                        artifact,
                    )
                ):
                    detail += ":conditions_pending"

                details.add(detail)

            errors.append(
                f"artifacts: {artifact_record_id}: операция {operation} не разрешена "
                f"определяющей записью прав (статусы: "
                f"{', '.join(sorted(details))})"
            )

    @classmethod
    def _right_permits(
        cls,
        right: dict[str, Any],
        fulfilments: dict[str, dict[str, Any]],
        work: dict[str, Any],
        artifact: dict[str, Any],
    ) -> bool:
        """Проверить, разрешает ли активная запись прав свою операцию."""

        return cls._right_permits_at(
            right,
            fulfilments,
            work,
            artifact,
            at=datetime.now(timezone.utc),
        )

    @classmethod
    def _right_permits_at(
        cls,
        right: dict[str, Any],
        fulfilments: dict[str, dict[str, Any]],
        work: dict[str, Any],
        artifact: dict[str, Any],
        *,
        at: datetime,
    ) -> bool:
        """Проверить право и его условия в заданный момент истории."""

        expires_at = right.get("rights_expires_at")

        if expires_at and date.fromisoformat(expires_at) < at.date():
            return False

        if right["status"] == "allowed":
            return True

        return (
            right["status"] == "conditional"
            and cls._conditions_satisfied_by_history(
                right,
                fulfilments,
                work,
                artifact,
                at=at,
            )
        )

    @staticmethod
    def _right_matches_operation_context(
        right: dict[str, Any],
        artifact: dict[str, Any],
        operation: str,
    ) -> bool:
        """Сопоставить режим права с режимом получения артефакта."""

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
        """Выбрать наиболее конкретные записи, определяющие операцию."""

        candidates = [
            item
            for item in rights
            if item["operation"] == operation
            and cls._right_matches_operation_context(item, artifact, operation)
        ]

        if not candidates:
            return []

        def specificity(item: dict[str, Any]) -> tuple[int, int]:
            """Вычислить приоритет области и уточнения режима получения."""

            mode_specificity = int(item["acquisition_method"] is not None)
            return SCOPE_SPECIFICITY[item["scope_type"]], mode_specificity

        controlling_specificity = max(specificity(item) for item in candidates)

        return [
            item
            for item in candidates
            if specificity(item) == controlling_specificity
        ]

    @classmethod
    def _event_operation_rights(
        cls,
        rights: list[dict[str, Any]],
        *,
        operation: str,
        event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Выбрать наиболее конкретные права для операции события получения."""

        candidates = [
            item
            for item in rights
            if item["operation"] == operation
            and (
                operation != "acquisition"
                or (
                    item["acquisition_method"] == event["acquisition_method"]
                    and item["acquisition_scope"] == event["acquisition_scope"]
                )
            )
        ]

        if not candidates:
            return []

        controlling_specificity = max(
            SCOPE_SPECIFICITY[item["scope_type"]] for item in candidates
        )

        return [
            item
            for item in candidates
            if SCOPE_SPECIFICITY[item["scope_type"]] == controlling_specificity
        ]

    @staticmethod
    def _validate_rights_supersedes(
        rights: dict[str, dict[str, Any]],
        errors: list[str],
    ) -> set[str]:
        """Проверить цепочки замены прав и вернуть ID заменённых записей."""

        superseded: set[str] = set()
        graph: dict[str, str] = {}
        successor_by_previous: dict[str, str] = {}

        for record_id, record in rights.items():
            previous = record.get("supersedes_rights_record_id")

            if not previous:
                continue

            if previous not in rights:
                errors.append(
                    f"rights: {record_id} ссылается на отсутствующую "
                    f"запись {previous!r}"
                )

                continue

            previous_record = rights[previous]

            comparable_fields = (
                "scope_type",
                "scope_id",
                "operation",
                "acquisition_method",
                "acquisition_scope",
                "derivative_scope",
            )

            if any(
                record[field] != previous_record[field]
                for field in comparable_fields
            ):
                errors.append(
                    f"rights: {record_id} заменяет {previous!r} с другой областью, "
                    "операцией или режимом получения"
                )

                continue

            if previous == record_id:
                errors.append(
                    f"rights: {record_id} не может заменять сам себя"
                )
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

            created = datetime.fromisoformat(
                record["created_at"].replace("Z", "+00:00")
            )

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
        """Проверить уникальность идентификаторов и связи дублей работ."""

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
                        f"works: {field}={value!r} принадлежит одновременно "
                        f"{previous!r} "
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
                        f"works: псевдоним {alias!r} принадлежит "
                        f"одновременно {owner!r} "
                        f"и {work_id!r}"
                    )

                identity_owners[alias] = work_id

            duplicate_of = work.get("duplicate_of_work_id")

            if not duplicate_of:
                continue

            if duplicate_of == work_id:
                errors.append(
                    f"works: {work_id!r} не может быть дубликатом самого себя"
                )

            elif duplicate_of not in works:
                errors.append(
                    f"works: {work_id!r} ссылается на отсутствующий "
                    "duplicate_of_work_id "
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
        """Проверить применимость области права к работе и артефакту."""

        scope_type = rights["scope_type"]
        scope_id = rights["scope_id"]

        expected = {
            "source_group": work["source_group_id"],
            "source": work["source_id"],
            "journal": work["journal_id"],
            "work": work["work_id"],
        }

        if scope_type == "artifact":
            return scope_id in {
                artifact["artifact_record_id"],
                artifact.get("artifact_id"),
            }

        if scope_type == "work":
            return scope_id in {work["work_id"], *work.get("work_aliases", [])}

        return expected[scope_type] == scope_id

    def _validate_artifact_payloads(
        self,
        artifacts: dict[str, dict[str, Any]],
        blob_specs: dict[str, _BlobSpec],
        retrieval_events: dict[str, dict[str, Any]],
    ) -> dict[str, int]:
        """Проверить файловые данные артефактов и событий получения."""

        artifact_paths: dict[str, dict[str, Any]] = {}
        payload_specs: dict[str, _BlobSpec] = {}

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
            artifact_spec = _BlobSpec(artifact["sha256"], artifact["bytes"])
            payload_specs[path_value] = artifact_spec
            spec = blob_specs.get(path_value)

            if spec is not None:
                if (
                    artifact.get("sha256") != spec.sha256
                    or artifact.get("bytes") != spec.size
                ):
                    raise ManifestError(
                        f"artifacts: {record_id}: запланированные байты "
                        "не совпадают с реестром"
                    )

            else:
                self._validate_artifact_file(artifact)

        for event in retrieval_events.values():
            path_value = event.get("response_path")

            if path_value is None:
                continue

            event_spec = _BlobSpec(
                event["response_sha256"],
                event["response_bytes"],
            )
            previous_spec = payload_specs.get(path_value)

            if previous_spec is not None and previous_spec != event_spec:
                raise ManifestConflictError(
                    f"retrieval_events: путь {path_value!r} связан "
                    "с разными байтами"
                )

            payload_specs[path_value] = event_spec
            planned_spec = blob_specs.get(path_value)

            if planned_spec is not None:
                if planned_spec != event_spec:
                    raise ManifestError(
                        f"retrieval_events: {event['retrieval_id']}: "
                        "запланированные байты не совпадают с событием"
                    )

            else:
                self._validate_retrieval_file(event)

        for path_value in blob_specs:
            if path_value not in payload_specs:
                raise ManifestError(
                    f"blob {path_value} не связан ни с артефактом, "
                    "ни с событием получения"
                )

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

    def _write_blobs(
        self,
        blobs: list[PlannedBlob],
        *,
        created_paths: list[Path],
    ) -> tuple[int, int]:
        """Атомарно записать новые файловые объекты без перезаписи существующих."""

        unique: dict[str, PlannedBlob] = {}

        for blob in blobs:
            actual_sha = sha256_bytes(blob.data)

            if actual_sha != blob.sha256:
                raise ManifestError(
                    f"blob {blob.relative_path}: заявлен SHA-256 "
                    f"{blob.sha256}, "
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
                existing_spec = _BlobSpec(blob.sha256, len(blob.data))

                if not self._file_matches(target, existing_spec):
                    raise ManifestConflictError(
                        f"Путь {blob.relative_path} занят другими байтами"
                    )

                unchanged += 1
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                dir=target.parent,
            )

            try:
                with os.fdopen(file_descriptor, "wb") as stream:
                    stream.write(blob.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # link создаёт конечное имя атомарно и никогда не
                    # перезаписывает уже существующий неизменяемый объект.
                    os.link(temporary_name, target)

                except FileExistsError:
                    if not self._file_matches(
                        target, _BlobSpec(blob.sha256, len(blob.data))
                    ):
                        raise ManifestConflictError(
                            f"Путь {blob.relative_path} конкурентно занят "
                            "другими байтами"
                        )

                    unchanged += 1
                    continue

            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

            written += 1
            created_paths.append(target)

        return written, unchanged

    @staticmethod
    def _remove_new_blobs(paths: list[Path]) -> None:
        """Удалить только blob-файлы, созданные незавершённым commit."""

        cleanup_errors: list[str] = []

        for path in paths:
            try:
                path.unlink(missing_ok=True)

            except OSError as exception:
                cleanup_errors.append(f"{path}: {exception}")

        if cleanup_errors:
            raise ManifestError(
                "Не удалось удалить blob-файлы незавершённой записи:\n"
                + "\n".join(cleanup_errors)
            )

    @staticmethod
    def _file_matches(path: Path, spec: _BlobSpec) -> bool:
        """Проверить тип, размер и SHA-256 существующего файла."""

        return (
            path.is_file() and
            path.stat().st_size == spec.size and
            sha256_file(path) == spec.sha256
        )

    def _resolve_data_path(self, relative_path: str) -> Path:
        """Разрешить относительный путь и запретить выход за каталог data/."""

        relative = Path(relative_path)

        if relative.is_absolute():
            raise ManifestError(
                f"Путь артефакта должен быть относительным: {relative_path}"
            )

        resolved = (self.project_root / relative).resolve()
        data_root = (self.project_root / "data").resolve()

        if not resolved.is_relative_to(data_root):
            raise ManifestError(f"Путь артефакта выходит за data/: {relative_path}")

        return resolved

    def _validate_artifact_file(self, artifact: dict[str, Any]) -> None:
        """Сверить зарегистрированный артефакт с локальным файлом."""

        path_value = artifact.get("path")

        if path_value is None:
            return

        path = self._resolve_data_path(path_value)
        record_id = artifact["artifact_record_id"]

        if not path.is_file():
            raise ManifestError(
                f"artifacts: {record_id}: файл не найден: {path_value}"
            )

        actual_size = path.stat().st_size

        if actual_size != artifact["bytes"]:
            raise ManifestError(
                f"artifacts: {record_id}: размер {actual_size}, "
                f"в реестре {artifact['bytes']}"
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

    def _validate_retrieval_file(self, event: dict[str, Any]) -> None:
        """Сверить тело HTTP-ответа с неизменяемым событием получения."""

        path_value = event.get("response_path")

        if path_value is None:
            return

        path = self._resolve_data_path(path_value)
        retrieval_id = event["retrieval_id"]

        if not path.is_file():
            raise ManifestError(
                f"retrieval_events: {retrieval_id}: файл не найден: {path_value}"
            )

        actual_size = path.stat().st_size

        if actual_size != event["response_bytes"]:
            raise ManifestError(
                f"retrieval_events: {retrieval_id}: размер {actual_size}, "
                f"в журнале {event['response_bytes']}"
            )

        if sha256_file(path) != event["response_sha256"]:
            raise ManifestError(
                f"retrieval_events: {retrieval_id}: SHA-256 файла "
                "не совпадает с журналом"
            )

    @staticmethod
    def _validate_timestamp_order(
        kind: str,
        record: dict[str, Any],
        *,
        updated_required: bool = True,
    ) -> None:
        """Проверить хронологический порядок временных полей записи."""

        created = datetime.fromisoformat(
            record["created_at"].replace("Z", "+00:00")
        )

        if kind == "rights":
            record_id = record[PRIMARY_FIELDS[kind]]
            checked = date.fromisoformat(record["rights_checked_at"])
            now = datetime.now(tz=created.tzinfo)

            if created > now:
                raise ManifestError(
                    f"rights: {record_id}: created_at находится в будущем"
                )

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
        """Отклонить циклы в цепочках родительских артефактов."""

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
                    raise ManifestError(
                        f"artifacts: цикл parent_artifact_id около {current}"
                    )

                seen.add(current)
                parent = by_artifact_id.get(current, {}).get("parent_artifact_id")
                current = parent

    @staticmethod
    def _atomic_replace_snapshot(
        path: Path,
        kind: str,
        records: dict[str, dict[str, Any]],
    ) -> None:
        """Атомарно заменить полный детерминированный снимок реестра."""

        path.parent.mkdir(parents=True, exist_ok=True)
        data = _snapshot_bytes(kind, records)
        existing = path.read_bytes() if path.exists() else None

        if existing == data:
            return

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )

        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temporary_name, path)

        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _atomic_append(path: Path, records: list[dict[str, Any]]) -> None:
        """Атомарно дополнить JSONL через замену целого файла."""

        if not records:
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_bytes() if path.exists() else b""

        if existing and not existing.endswith(b"\n"):
            raise ManifestError(f"{path}: отсутствует конечный перевод строки")

        addition = "".join(f"{canonical_json(record)}\n" for record in records).encode(
            "utf-8"
        )

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )

        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(existing)
                stream.write(addition)
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temporary_name, path)

        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _restore_registry_files(originals: dict[Path, bytes | None]) -> None:
        """Восстановить реестры после незавершённой многофайловой записи."""

        restoration_errors: list[str] = []

        for path, data in originals.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                    continue

                path.parent.mkdir(parents=True, exist_ok=True)
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{path.name}.rollback.",
                    dir=path.parent,
                )

                try:
                    with os.fdopen(file_descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())

                    os.replace(temporary_name, path)

                finally:
                    if os.path.exists(temporary_name):
                        os.unlink(temporary_name)

            except OSError as exception:
                restoration_errors.append(f"{path}: {exception}")

        if restoration_errors:
            raise ManifestError(
                "Не удалось восстановить реестры после ошибки записи:\n"
                + "\n".join(restoration_errors)
            )

    @contextmanager
    def _exclusive_lock(self) -> Generator[None, None, None]:
        """Удерживать эксклюзивную межпроцессную блокировку реестров."""

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
