"""Модульные тесты хранилища реестров корпуса."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from src.collect.base import Document
from src.corpus.manifests import (
    ManifestConcurrencyError,
    ManifestConflictError,
    ManifestError,
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
    canonical_json,
    sha256_bytes,
)
from src.corpus.profiles import get_source_profile
from src.corpus.schema_validation import SchemaValidationError

ROOT = Path(__file__).resolve().parents[1]
TEST_TIMESTAMP = "2026-08-27T12:00:00+03:00"


class ManifestStoreTests(unittest.TestCase):
    """Проверки предварительной валидации и записи реестров корпуса."""

    def setUp(self) -> None:
        """Создать изолированное временное хранилище для теста."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        (self.project_root / "data" / "raw" / "pdf").mkdir(parents=True)
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )
        self.profile = get_source_profile("ufn")

    def tearDown(self) -> None:
        """Удалить временное хранилище после теста."""

        self.temporary_directory.cleanup()

    def document(self, **changes: Any) -> Document:
        """Создать синтетический документ с выбранными изменениями."""

        values: dict[str, Any] = {
            "source": "ufn.ru",
            "url": "https://ufn.ru/ru/articles/2024/1/a/",
            "title": "Квантовые свойства плазмы",
            "text": "Русский физический текст " * 8,
            "authors": ["Иванов И. И."],
            "section": "Обзоры актуальных проблем",
            "extra": {"year": "2024", "text_source": "html"},
        }
        values.update(changes)
        return Document(**values)

    def right(
        self,
        operation: str,
        *,
        status: str = "allowed",
        expires: str | None = None,
    ) -> dict[str, Any]:
        """Создать синтетическую запись о праве на операцию."""

        return {
            "schema_version": "rights-v1",
            "created_at": TEST_TIMESTAMP,
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": status,
            "access_basis": "Синтетическое разрешение для модульного теста.",
            "basis_type": "explicit_license",
            "acquisition_method": (
                "manual_download" if operation == "acquisition" else None
            ),
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2026-08-27",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": expires,
            "supersedes_rights_record_id": None,
        }

    def plan(
        self,
        document: Document | None = None,
        *,
        include_rights: bool = True,
        rights: list[dict[str, Any]] | None = None,
    ) -> ManifestPlan:
        """Создать синтетический план без зависимости от старого импорта."""

        selected_document = document or self.document()
        selected_rights = rights if rights is not None else [
            self.right("acquisition"),
            self.right("storage"),
        ]
        rights_ids = (
            [item["rights_record_id"] for item in selected_rights]
            if include_rights
            else []
        )

        work_id = "source:S01_UFN_RU:2024/1/a"
        text_bytes = selected_document.text.encode("utf-8")
        text_sha256 = sha256_bytes(text_bytes)
        relative_path = f"data/extracted/tests/{text_sha256}.txt"

        work = {
            "schema_version": "works-v1",
            "created_at": TEST_TIMESTAMP,
            "work_id": work_id,
            "work_aliases": [],
            "source_group_id": self.profile.source_group_id,
            "source_id": self.profile.source_id,
            "platform": self.profile.platform,
            "journal_id": self.profile.journal_id,
            "journal_title": self.profile.journal_title,
            "canonical_url": selected_document.url,
            "doi": None,
            "edn": None,
            "identity_confidence": "medium",
            "title": selected_document.title,
            "authors": selected_document.authors,
            "abstract": None,
            "keywords": [],
            "published_at": "2024-01-01",
            "language": "ru",
            "genre": "review_article",
            "pacs_codes_raw": [],
            "udc_codes_raw": [],
            "publisher_section": selected_document.section,
            "duplicate_of_work_id": None,
            "eligibility_status": "pending",
            "exclusion_reason": None,
            "updated_at": TEST_TIMESTAMP,
        }

        artifact = {
            "schema_version": "artifacts-v1",
            "created_at": TEST_TIMESTAMP,
            "artifact_record_id": f"artifact-record:test:{text_sha256}",
            "artifact_id": f"sha256:{text_sha256}",
            "work_id": work_id,
            "parent_artifact_id": None,
            "retrievals": [
                {
                    "retrieval_id": f"retrieval:test:{text_sha256}",
                    "retrieved_url": selected_document.url,
                    "retrieved_at": TEST_TIMESTAMP,
                    "response_metadata_sha256": "b" * 64,
                }
            ],
            "rights_record_ids": rights_ids,
            "content_role": "metadata_only",
            "representation": "plain_text",
            "mime_type": "text/plain; charset=utf-8",
            "path": relative_path,
            "sha256": text_sha256,
            "bytes": len(text_bytes),
            "extraction_method": "test_fixture",
            "extraction_version": "test-v1",
            "ocr_method": None,
            "ocr_version": None,
            "preprocessing_version": None,
            "tokenizer_repo": None,
            "tokenizer_revision": None,
            "characters": len(selected_document.text),
            "words": len(selected_document.text.split()),
            "subtokens": None,
            "h2_input_sha256": None,
            "label_leakage_audit_version": None,
            "label_leakage_audit_status": "not_checked",
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "acquisition_status": "retrieved",
            "extraction_status": "succeeded",
            "qa_status": "not_evaluated",
            "processing_status": "processed",
            "error_code": None,
            "error_detail": None,
            "updated_at": TEST_TIMESTAMP,
        }

        return ManifestPlan(
            rights=selected_rights if include_rights else [],
            works=[work],
            artifacts=[artifact],
            blobs=[PlannedBlob(relative_path, text_bytes, text_sha256)],
        )

    def test_commit_writes_utf8_content_addressed_file(self) -> None:
        """Запись должна сохранять UTF-8-файл по адресу на основе хеша."""

        plan = self.plan()
        result = self.store.commit(plan)
        self.assertEqual(result.inserted["works"], 1)
        self.assertEqual(result.inserted["artifacts"], 1)
        self.assertEqual(result.written_blobs, 1)

        artifact = self.store.records("artifacts")[0]
        stored = self.project_root / artifact["path"]
        self.assertEqual(stored.read_bytes(), self.document().text.encode("utf-8"))
        self.assertEqual(artifact["artifact_id"], f"sha256:{artifact['sha256']}")
        self.assertEqual(artifact["content_role"], "metadata_only")
        manifest_text = (self.project_root / "manifests" / "works.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("Квантовые свойства", manifest_text)
        self.assertNotIn("\\u041a", manifest_text)
        self.assertTrue(manifest_text.endswith("\n"))

    def test_exact_second_commit_is_noop(self) -> None:
        """Повторная запись неизменного плана не должна менять хранилище."""

        plan = self.plan()
        self.store.commit(plan)
        second = self.store.commit(plan)
        self.assertTrue(all(count == 0 for count in second.inserted.values()))
        self.assertTrue(all(count == 0 for count in second.updated.values()))
        self.assertEqual(second.unchanged["works"], 1)
        self.assertEqual(second.unchanged["artifacts"], 1)
        manifest_path = self.project_root / "manifests" / "works.jsonl"
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_same_work_id_with_changed_record_is_conflict(self) -> None:
        """Изменённая запись с прежним work_id должна создавать конфликт."""

        self.store.commit(self.plan())
        changed = self.document(title="Другое название")
        # Синтетический план намеренно сохраняет тот же work_id.
        with self.assertRaises(ManifestConflictError):
            self.store.commit(self.plan(changed))

    def test_unknown_right_reference_aborts_before_writing(self) -> None:
        """Неизвестная ссылка на право должна отменять запись целиком."""

        plan = self.plan(include_rights=False)
        plan.artifacts[0]["rights_record_ids"] = ["missing-right"]
        with self.assertRaises(ManifestError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())
        self.assertFalse((self.project_root / plan.blobs[0].relative_path).exists())

    def test_schema_error_aborts_before_writing(self) -> None:
        """Ошибка схемы должна обнаруживаться до изменения хранилища."""

        plan = self.plan()
        plan.works[0] = copy.deepcopy(plan.works[0])
        plan.works[0]["language"] = "r"
        with self.assertRaises(SchemaValidationError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())

    def test_audit_checks_real_hash(self) -> None:
        """Проверка хранилища должна сверять фактический SHA-256 файла."""

        self.store.commit(self.plan())
        report = self.store.audit()
        self.assertTrue(report.ok)
        self.assertEqual(report.counts["works"], 1)

        artifact = self.store.records("artifacts")[0]
        (self.project_root / artifact["path"]).write_bytes(b"changed")
        broken = self.store.audit()
        self.assertFalse(broken.ok)
        has_integrity_error = any(
            "размер" in error or "SHA-256" in error for error in broken.errors
        )

        self.assertTrue(has_integrity_error)

    def test_empty_store_does_not_report_success(self) -> None:
        """Пустое хранилище не должно считаться успешно проверенным."""

        report = self.store.audit()
        self.assertFalse(report.ok)
        self.assertTrue(any("отсутствуют" in error for error in report.errors))

    def test_preflight_without_rights_needs_explicit_inventory_mode(self) -> None:
        """План без прав допустим только в явно выбранном режиме инвентаризации."""

        plan = self.plan(include_rights=False)
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])
        result = self.store.preflight([plan], allow_unresolved_rights=True)
        self.assertEqual(result.inserted["works"], 1)
        self.assertFalse((self.project_root / "manifests").exists())

    def test_batch_preflight_detects_cross_line_conflict_without_writes(self) -> None:
        """Предварительная проверка пакета должна находить конфликт между планами."""

        first = self.plan()
        changed_document = self.document(title="Другое название той же статьи")
        second = self.plan(changed_document)
        with self.assertRaises(ManifestConflictError):
            self.store.preflight([first, second])
        self.assertFalse((self.project_root / "manifests").exists())

    def test_prohibited_acquisition_right_blocks_commit(self) -> None:
        """Запрет на получение данных должен блокировать запись плана."""

        rights = [self.right("acquisition", status="prohibited"), self.right("storage")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_wrong_operation_cannot_substitute_for_acquisition(self) -> None:
        """Право на другую операцию не должно заменять право на получение."""

        redistribution = self.right("acquisition")
        redistribution["rights_record_id"] = "right-redistribution"
        redistribution["operation"] = "redistribution"
        redistribution["acquisition_method"] = None
        redistribution["acquisition_scope"] = None
        rights = [redistribution, self.right("storage")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_conflicting_active_rights_are_conservative(self) -> None:
        """Противоречивые активные права должны трактоваться консервативно."""

        blocked = self.right("acquisition", status="prohibited")
        blocked["rights_record_id"] = "right-acquisition-blocked"
        rights = [self.right("acquisition"), blocked, self.right("storage")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_more_specific_unreferenced_prohibition_blocks(self) -> None:
        """Более точный запрет должен действовать без прямой ссылки артефакта."""

        plan = self.plan()
        blocked = self.right("acquisition", status="prohibited")
        blocked["rights_record_id"] = "right-work-acquisition-blocked"
        blocked["scope_type"] = "work"
        blocked["scope_id"] = plan.works[0]["work_id"]
        # Запрет намеренно не включён в rights_record_ids.
        plan.rights.append(blocked)
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_later_acquisition_prohibition_does_not_rewrite_history(self) -> None:
        """Новый запрет должен блокировать будущее, а не прошлое получение."""

        initial = self.plan()
        self.store.commit(initial)
        retrieval_count = len(self.store.records("retrieval_events"))
        decision_count = len(self.store.records("operation_decisions"))
        blocked = self.right("acquisition", status="prohibited")
        blocked.update(
            {
                "created_at": "2026-08-28T12:00:00+03:00",
                "rights_checked_at": "2026-08-28",
                "rights_record_id": "right-work-acquisition-later-blocked",
                "scope_type": "work",
                "scope_id": initial.works[0]["work_id"],
                "access_basis": "Более поздний точечный запрет.",
            }
        )

        result = self.store.commit(ManifestPlan(rights=[blocked]))

        self.assertEqual(result.inserted["rights"], 1)
        self.assertEqual(
            len(self.store.records("retrieval_events")),
            retrieval_count,
        )
        self.assertEqual(
            len(self.store.records("operation_decisions")),
            decision_count,
        )
        self.assertTrue(self.store.audit().ok)

        future_event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:after-work-prohibition",
            "created_at": "2026-08-29T12:00:00+03:00",
            "request_context_type": "work",
            "request_context_id": initial.works[0]["work_id"],
            "source_group_id": None,
            "requested_url": initial.works[0]["canonical_url"],
            "final_url": None,
            "retrieved_at": "2026-08-29T12:00:00+03:00",
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": ["right-acquisition"],
            "http_status": None,
            "response_headers": {},
            "response_metadata_sha256": "d" * 64,
            "response_path": None,
            "response_sha256": None,
            "response_bytes": None,
            "outcome": "metadata_only",
            "error_code": None,
            "error_detail": None,
        }

        with self.assertRaises(ManifestError):
            self.store.preflight(
                [ManifestPlan(retrieval_events=[future_event])]
            )

    def test_retrieval_rights_are_resolved_at_retrieved_at(self) -> None:
        """Поздняя запись события не должна менять права в момент получения."""

        body = b"historical response"
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.bin"
        acquisition = self.right("acquisition")
        storage = self.right("storage")
        blocked = self.right("acquisition", status="prohibited")
        blocked.update(
            {
                "created_at": "2026-08-28T12:00:00+03:00",
                "rights_checked_at": "2026-08-28",
                "rights_record_id": "right-acquisition-later-blocked",
                "access_basis": "Запрет, появившийся после получения ответа.",
            }
        )
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:recorded-later",
            "created_at": "2026-08-29T12:00:00+03:00",
            "request_context_type": "source",
            "request_context_id": self.profile.source_id,
            "source_group_id": self.profile.source_group_id,
            "requested_url": "https://example.invalid/historical",
            "final_url": "https://example.invalid/historical",
            "retrieved_at": TEST_TIMESTAMP,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [
                acquisition["rights_record_id"],
                storage["rights_record_id"],
            ],
            "http_status": 200,
            "response_headers": {},
            "response_metadata_sha256": "e" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }
        plan = ManifestPlan(
            rights=[acquisition, storage, blocked],
            retrieval_events=[event],
            blobs=[PlannedBlob(path, body, digest)],
        )

        result = self.store.commit(plan)

        self.assertEqual(result.inserted["retrieval_events"], 1)
        decisions = [
            item
            for item in self.store.records("operation_decisions")
            if item["subject_id"] == event["retrieval_id"]
        ]
        acquisition_decision = next(
            item for item in decisions if item["operation"] == "acquisition"
        )
        self.assertEqual(acquisition_decision["decision_at"], TEST_TIMESTAMP)
        self.assertEqual(acquisition_decision["status"], "allowed")

    def test_backfilled_artifact_uses_historical_acquisition_rights(self) -> None:
        """Старое получение артефакта должно проверяться в момент получения."""

        acquisition = self.right("acquisition")
        storage = self.right("storage")
        blocked = self.right("acquisition", status="prohibited")
        blocked.update(
            {
                "created_at": "2026-08-28T12:00:00+03:00",
                "rights_checked_at": "2026-08-28",
                "rights_record_id": "right-acquisition-after-backfill",
                "access_basis": "Запрет после фактического получения.",
            }
        )
        plan = self.plan(rights=[acquisition, storage, blocked])
        artifact = plan.artifacts[0]
        artifact["created_at"] = "2026-08-29T12:00:00+03:00"
        artifact["updated_at"] = "2026-08-29T12:00:00+03:00"

        result = self.store.commit(plan)

        self.assertEqual(result.inserted["artifacts"], 1)
        decisions = [
            item
            for item in self.store.records("operation_decisions")
            if item["subject_type"] == "artifact"
            and item["subject_id"] == artifact["artifact_record_id"]
            and item["operation"] == "acquisition"
        ]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision_at"], TEST_TIMESTAMP)
        self.assertEqual(decisions[0]["status"], "allowed")

    def test_artifact_ignores_failed_attempt_when_selecting_acquisition_time(
        self,
    ) -> None:
        """Решение артефакта должно опираться на первое успешное получение."""

        blocked = self.right("acquisition", status="prohibited")
        blocked.update(
            {
                "created_at": "2026-08-27T09:00:00+03:00",
                "rights_record_id": "right-acquisition-initially-blocked",
                "access_basis": "Изначальный запрет для проверки повторной попытки.",
            }
        )
        allowed = self.right("acquisition")
        allowed.update(
            {
                "created_at": "2026-08-28T09:00:00+03:00",
                "rights_checked_at": "2026-08-28",
                "rights_record_id": "right-acquisition-later-allowed",
                "access_basis": "Позднее разрешение повторной попытки.",
                "supersedes_rights_record_id": blocked["rights_record_id"],
            }
        )
        storage = self.right("storage")
        plan = self.plan(rights=[blocked, allowed, storage])
        artifact = plan.artifacts[0]
        failed_at = "2026-08-27T10:00:00+03:00"
        succeeded_at = "2026-08-28T12:00:00+03:00"
        failed_id = "retrieval:test:failed-before-permission"
        succeeded_id = "retrieval:test:succeeded-after-permission"
        artifact["created_at"] = "2026-08-29T12:00:00+03:00"
        artifact["updated_at"] = "2026-08-29T12:00:00+03:00"
        artifact["retrievals"] = [
            {
                "retrieval_id": failed_id,
                "retrieved_url": artifact["retrievals"][0]["retrieved_url"],
                "retrieved_at": failed_at,
                "response_metadata_sha256": "e" * 64,
            },
            {
                "retrieval_id": succeeded_id,
                "retrieved_url": artifact["retrievals"][0]["retrieved_url"],
                "retrieved_at": succeeded_at,
                "response_metadata_sha256": "f" * 64,
            },
        ]
        common = {
            "schema_version": "retrieval-events-v1",
            "request_context_type": "artifact",
            "request_context_id": artifact["artifact_record_id"],
            "source_group_id": None,
            "requested_url": plan.works[0]["canonical_url"],
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "response_headers": {},
        }
        failed_event = {
            **common,
            "retrieval_id": failed_id,
            "created_at": failed_at,
            "final_url": None,
            "retrieved_at": failed_at,
            "rights_record_ids": [blocked["rights_record_id"]],
            "http_status": 403,
            "response_metadata_sha256": "e" * 64,
            "response_path": None,
            "response_sha256": None,
            "response_bytes": None,
            "outcome": "failed",
            "error_code": "http_403",
            "error_detail": "Получение запрещено.",
        }
        succeeded_event = {
            **common,
            "retrieval_id": succeeded_id,
            "created_at": succeeded_at,
            "final_url": plan.works[0]["canonical_url"],
            "retrieved_at": succeeded_at,
            "rights_record_ids": [
                allowed["rights_record_id"],
                storage["rights_record_id"],
            ],
            "http_status": 200,
            "response_metadata_sha256": "f" * 64,
            "response_path": artifact["path"],
            "response_sha256": artifact["sha256"],
            "response_bytes": artifact["bytes"],
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }
        plan.retrieval_events = [failed_event, succeeded_event]

        result = self.store.commit(plan)

        self.assertEqual(result.inserted["artifacts"], 1)
        acquisition_decision = next(
            item
            for item in self.store.records("operation_decisions")
            if item["subject_type"] == "artifact"
            and item["subject_id"] == artifact["artifact_record_id"]
            and item["operation"] == "acquisition"
        )
        self.assertEqual(acquisition_decision["decision_at"], succeeded_at)
        self.assertEqual(acquisition_decision["status"], "allowed")

    def test_retrieved_artifact_requires_nonfailed_retrieval(self) -> None:
        """Статус retrieved нельзя вывести только из неудачной попытки."""

        blocked = self.right("acquisition", status="prohibited")
        blocked.update(
            {
                "created_at": "2026-08-27T09:00:00+03:00",
                "rights_record_id": "right-acquisition-failed-only",
                "access_basis": "Запрет единственной неудачной попытки.",
            }
        )
        allowed = self.right("acquisition")
        allowed.update(
            {
                "created_at": "2026-08-28T09:00:00+03:00",
                "rights_checked_at": "2026-08-28",
                "rights_record_id": "right-acquisition-after-failure",
                "access_basis": "Разрешение, появившееся после неудачи.",
                "supersedes_rights_record_id": blocked["rights_record_id"],
            }
        )
        storage = self.right("storage")
        plan = self.plan(rights=[blocked, allowed, storage])
        artifact = plan.artifacts[0]
        failed_at = "2026-08-27T10:00:00+03:00"
        failed_id = "retrieval:test:failed-only"
        artifact["created_at"] = "2026-08-29T12:00:00+03:00"
        artifact["updated_at"] = "2026-08-29T12:00:00+03:00"
        artifact["retrievals"] = [
            {
                "retrieval_id": failed_id,
                "retrieved_url": plan.works[0]["canonical_url"],
                "retrieved_at": failed_at,
                "response_metadata_sha256": "e" * 64,
            }
        ]
        plan.retrieval_events = [
            {
                "schema_version": "retrieval-events-v1",
                "retrieval_id": failed_id,
                "created_at": failed_at,
                "request_context_type": "artifact",
                "request_context_id": artifact["artifact_record_id"],
                "source_group_id": None,
                "requested_url": plan.works[0]["canonical_url"],
                "final_url": None,
                "retrieved_at": failed_at,
                "acquisition_method": "manual_download",
                "acquisition_scope": "sample",
                "rights_record_ids": [blocked["rights_record_id"]],
                "http_status": 403,
                "response_headers": {},
                "response_metadata_sha256": "e" * 64,
                "response_path": None,
                "response_sha256": None,
                "response_bytes": None,
                "outcome": "failed",
                "error_code": "http_403",
                "error_detail": "Получение запрещено.",
            }
        ]

        with self.assertRaisesRegex(
            ManifestError,
            "не имеет состоявшегося события получения",
        ):
            self.store.commit(plan)

        self.assertFalse(self.store.path_for("artifacts").exists())

    def test_prohibition_scoped_to_work_alias_blocks(self) -> None:
        """Запрет для прежнего псевдонима работы должен блокировать операцию."""

        plan = self.plan()
        plan.works[0]["work_aliases"] = ["work:old-source-id"]
        blocked = self.right("acquisition", status="prohibited")
        blocked["rights_record_id"] = "right-work-alias-acquisition-blocked"
        blocked["scope_type"] = "work"
        blocked["scope_id"] = "work:old-source-id"
        plan.rights.append(blocked)
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_more_specific_permission_overrides_broader_prohibition(self) -> None:
        """Точное разрешение для работы должно отменять общий запрет."""

        blocked = self.right("acquisition", status="prohibited")
        allowed = self.right("acquisition")
        allowed["rights_record_id"] = "right-work-acquisition-allowed"
        plan = self.plan(rights=[blocked, allowed, self.right("storage")])
        allowed["scope_type"] = "work"
        allowed["scope_id"] = plan.works[0]["work_id"]
        result = self.store.preflight([plan])
        self.assertEqual(result.inserted["works"], 1)

    def test_acquisition_right_must_match_actual_mode(self) -> None:
        """Разрешённый способ получения должен совпадать с фактическим."""

        acquisition = self.right("acquisition")
        acquisition["acquisition_method"] = "crawler"
        acquisition["acquisition_scope"] = "bulk"
        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[acquisition, self.right("storage")])]
            )

    def test_conditional_right_requires_fulfilment_evidence(self) -> None:
        """Условное право должно иметь дату и подтверждение выполнения условий."""

        conditional = self.right("acquisition", status="conditional")
        conditional["rights_conditions"] = ["Получить письменное разрешение."]

        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[conditional, self.right("storage")])]
            )

        timestamp_only = copy.deepcopy(conditional)
        timestamp_only["conditions_satisfied_at"] = TEST_TIMESTAMP
        with self.assertRaises(SchemaValidationError):
            self.store.preflight(
                [self.plan(rights=[timestamp_only, self.right("storage")])]
            )

        evidence_only = copy.deepcopy(conditional)
        evidence_only["conditions_evidence_sha256"] = "b" * 64
        with self.assertRaises(SchemaValidationError):
            self.store.preflight(
                [self.plan(rights=[evidence_only, self.right("storage")])]
            )

        fulfilled = copy.deepcopy(conditional)
        fulfilled["conditions_satisfied_at"] = TEST_TIMESTAMP
        fulfilled["conditions_evidence_sha256"] = "b" * 64
        result = self.store.preflight(
            [self.plan(rights=[fulfilled, self.right("storage")])]
        )
        self.assertEqual(result.inserted["artifacts"], 1)

    def test_condition_history_is_primary_for_conditional_right(self) -> None:
        """Отдельное выполнение точного условия должно разрешать операцию."""

        conditional = self.right("acquisition", status="conditional")
        condition = "Получить письменное разрешение."
        conditional["rights_conditions"] = [condition]
        fulfilment = {
            "schema_version": "condition-fulfilments-v1",
            "fulfilment_id": "fulfilment:test:permission",
            "created_at": TEST_TIMESTAMP,
            "rights_record_id": conditional["rights_record_id"],
            "condition": condition,
            "subject_type": "source",
            "subject_id": self.profile.source_id,
            "status": "satisfied",
            "satisfied_at": TEST_TIMESTAMP,
            "expires_at": None,
            "evidence_sha256": "e" * 64,
            "supersedes_fulfilment_id": None,
        }
        plan = self.plan(rights=[conditional, self.right("storage")])
        plan.condition_fulfilments.append(fulfilment)

        result = self.store.commit(plan)

        self.assertEqual(result.inserted["condition_fulfilments"], 1)
        decisions = self.store.records("operation_decisions")
        acquisition = [
            item for item in decisions if item["operation"] == "acquisition"
        ]
        self.assertEqual(acquisition[0]["status"], "allowed")
        self.assertEqual(
            acquisition[0]["condition_fulfilment_ids"],
            [fulfilment["fulfilment_id"]],
        )

    def test_condition_for_another_project_does_not_permit_operation(self) -> None:
        """Выполнение условия чужого проекта не должно действовать в ruPhysBERT."""

        conditional = self.right("acquisition", status="conditional")
        condition = "Использовать только в согласованном проекте."
        conditional["rights_conditions"] = [condition]
        plan = self.plan(rights=[conditional, self.right("storage")])
        plan.condition_fulfilments.append(
            {
                "schema_version": "condition-fulfilments-v1",
                "fulfilment_id": "fulfilment:test:foreign-project",
                "created_at": TEST_TIMESTAMP,
                "rights_record_id": conditional["rights_record_id"],
                "condition": condition,
                "subject_type": "project",
                "subject_id": "another-project",
                "status": "satisfied",
                "satisfied_at": TEST_TIMESTAMP,
                "expires_at": None,
                "evidence_sha256": "f" * 64,
                "supersedes_fulfilment_id": None,
            }
        )

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_saved_artifact_requires_storage(self) -> None:
        """Сохранение артефакта должно требовать права на хранение."""

        without_storage = self.plan(rights=[self.right("acquisition")])

        with self.assertRaises(ManifestError):
            self.store.preflight([without_storage])

        self.store.preflight([self.plan()])

    def test_supersedes_requires_later_record(self) -> None:
        """Замещающая запись о праве должна быть создана позже исходной."""

        previous = self.right("acquisition", status="prohibited")
        previous["created_at"] = "2026-08-27T12:00:00+03:00"
        successor = self.right("acquisition")
        successor["rights_record_id"] = "right-acquisition-successor"
        successor["supersedes_rights_record_id"] = previous["rights_record_id"]
        successor["created_at"] = previous["created_at"]
        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[previous, successor, self.right("storage")])]
            )

    def test_later_successor_replaces_previous_prohibition(self) -> None:
        """Более позднее разрешение должно замещать прежний запрет."""

        previous = self.right("acquisition", status="prohibited")
        previous["created_at"] = "2026-08-27T11:00:00+03:00"
        successor = self.right("acquisition")
        successor["rights_record_id"] = "right-acquisition-successor"
        successor["supersedes_rights_record_id"] = previous["rights_record_id"]
        successor["created_at"] = TEST_TIMESTAMP
        result = self.store.preflight(
            [self.plan(rights=[previous, successor, self.right("storage")])]
        )
        self.assertEqual(result.inserted["artifacts"], 1)

    def test_expired_storage_right_blocks_processed_text(self) -> None:
        """Истёкшее право на хранение должно блокировать обработанный текст."""

        rights = [
            self.right("acquisition"),
            self.right("storage", expires="2025-01-01"),
        ]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_future_right_is_rejected(self) -> None:
        """Запись о праве с датой из будущего должна отклоняться."""

        future = self.right("acquisition")
        future["created_at"] = "2099-01-02T12:00:00+03:00"
        future["rights_checked_at"] = "2099-01-02"
        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[future, self.right("storage")])]
            )

    def test_no_public_blob_staging_without_manifest_plan(self) -> None:
        """Хранилище не должно принимать файлы в обход плана реестров."""

        self.assertFalse(hasattr(self.store, "stage_blobs"))

    def test_created_at_after_updated_at_is_rejected_before_write(self) -> None:
        """Дата создания позже даты обновления должна отклоняться до записи."""

        plan = self.plan()
        plan.works[0]["created_at"] = "2026-08-28T12:00:00+03:00"
        with self.assertRaises(ManifestError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())

    def test_missing_planned_blob_is_rejected_before_write(self) -> None:
        """Отсутствующий в плане файл должен обнаруживаться до записи."""

        plan = self.plan()
        plan.blobs.clear()
        with self.assertRaises(ManifestError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())

    def test_artifact_id_mismatch_is_rejected_before_write(self) -> None:
        """Несоответствие artifact_id содержимому должно отклоняться до записи."""

        plan = self.plan()
        plan.artifacts[0]["artifact_id"] = f"sha256:{'0' * 64}"
        with self.assertRaises(ManifestError):
            self.store.commit(plan)

    def test_parent_cycle_is_rejected_before_write(self) -> None:
        """Циклическая родительская связь артефактов должна отклоняться."""

        plan = self.plan()
        plan.artifacts[0]["parent_artifact_id"] = plan.artifacts[0]["artifact_id"]
        with self.assertRaises(ManifestError):
            self.store.commit(plan)

    def test_different_import_timestamp_is_still_noop(self) -> None:
        """Другое время импорта не должно менять равнозначную запись."""

        first = self.plan()
        self.store.commit(first)
        second = self.plan()
        second.works[0]["created_at"] = "2026-08-28T12:00:00+03:00"
        second.works[0]["updated_at"] = "2026-08-28T12:00:00+03:00"
        second.artifacts[0]["created_at"] = "2026-08-28T12:00:00+03:00"
        second.artifacts[0]["updated_at"] = "2026-08-28T12:00:00+03:00"
        result = self.store.commit(second)
        self.assertEqual(result.unchanged["works"], 1)
        self.assertEqual(result.unchanged["artifacts"], 1)

    def test_jsonl_is_one_object_per_line(self) -> None:
        """Каждая строка JSONL должна содержать ровно один объект JSON."""

        self.store.commit(self.plan())
        path = self.project_root / "manifests" / "artifacts.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_work_update_replaces_snapshot_with_matching_cas(self) -> None:
        """Обновление с причиной и верным хешем должно заменить одну строку."""

        self.store.commit(self.plan())
        expected_hashes = self.store.snapshot_hashes()
        updated = self.plan()
        updated.works[0]["abstract"] = "Проверенная аннотация статьи."
        updated.works[0]["updated_at"] = "2026-08-28T12:00:00+03:00"
        work_id = updated.works[0]["work_id"]
        updated.work_update_reasons[work_id] = "Добавлена проверенная аннотация."

        result = self.store.commit(
            updated,
            expected_snapshot_hashes=expected_hashes,
        )

        self.assertEqual(result.updated["works"], 1)
        self.assertEqual(
            self.store.records("works")[0]["abstract"],
            "Проверенная аннотация статьи.",
        )
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(len(self.store.records("work_revisions")), 2)

    def test_stale_snapshot_hash_rejects_update_without_writes(self) -> None:
        """Устаревший CAS-хеш не должен менять снимок или журнал ревизий."""

        self.store.commit(self.plan())
        previous_bytes = self.store.path_for("works").read_bytes()
        previous_revision_count = len(self.store.records("work_revisions"))
        updated = self.plan()
        updated.works[0]["abstract"] = "Новая проверенная аннотация."
        updated.works[0]["updated_at"] = "2026-08-28T12:00:00+03:00"
        updated.work_update_reasons[updated.works[0]["work_id"]] = "Уточнение."

        with self.assertRaises(ManifestConflictError):
            self.store.commit(
                updated,
                expected_snapshot_hashes={"works": "0" * 64},
            )

        self.assertEqual(self.store.path_for("works").read_bytes(), previous_bytes)
        self.assertEqual(
            len(self.store.records("work_revisions")),
            previous_revision_count,
        )

    def test_cas_wins_over_conflicting_concurrent_insert(self) -> None:
        """Гонка вставки должна сообщаться как ошибка конкурентного снимка."""

        expected_hashes = self.store.snapshot_hashes()
        concurrent = self.plan(self.document(title="Название другого процесса"))
        self.store.commit(concurrent)

        with self.assertRaises(ManifestConcurrencyError):
            self.store.commit(
                self.plan(),
                expected_snapshot_hashes=expected_hashes,
            )

    def test_artifact_update_creates_revision_and_new_decisions(self) -> None:
        """Изменение артефакта должно оставить ревизию и решения операций."""

        self.store.commit(self.plan())
        initial_decisions = len(self.store.records("operation_decisions"))
        updated = self.plan()
        artifact = updated.artifacts[0]
        artifact["qa_status"] = "passed"
        artifact["updated_at"] = "2026-08-28T12:00:00+03:00"
        updated.artifact_update_reasons[
            artifact["artifact_record_id"]
        ] = "Проверено качество текста."

        result = self.store.commit(
            updated,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        self.assertEqual(result.updated["artifacts"], 1)
        self.assertEqual(len(self.store.records("artifact_revisions")), 2)
        self.assertGreater(
            len(self.store.records("operation_decisions")),
            initial_decisions,
        )

    def test_inline_retrieval_is_migrated_to_history(self) -> None:
        """Старый встроенный retrieval должен стать событием metadata_only."""

        self.store.commit(self.plan())
        events = self.store.records("retrieval_events")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "metadata_only")
        self.assertIsNone(events[0]["http_status"])
        self.assertIsNone(events[0]["final_url"])

    def test_late_doi_preserves_work_id_and_requires_verified_alias(self) -> None:
        """Поздний DOI должен стать проверенным псевдонимом прежней работы."""

        self.store.commit(self.plan())
        updated = self.plan()
        work = updated.works[0]
        original_work_id = work["work_id"]
        work["doi"] = "10.1000/late-doi"
        work["work_aliases"] = ["doi:10.1000/late-doi"]
        work["updated_at"] = "2026-08-28T12:00:00+03:00"
        updated.work_update_reasons[original_work_id] = "Найден и проверен DOI."
        retrieval_id = updated.artifacts[0]["retrievals"][0]["retrieval_id"]
        updated.work_aliases.append(
            {
                "schema_version": "work-aliases-v1",
                "alias_record_id": "alias:test:late-doi",
                "created_at": "2026-08-28T12:00:00+03:00",
                "work_id": original_work_id,
                "alias_type": "doi",
                "alias_value": "10.1000/late-doi",
                "verified_at": "2026-08-28T12:00:00+03:00",
                "evidence_sha256": "c" * 64,
                "source_retrieval_id": retrieval_id,
                "supersedes_alias_record_id": None,
            }
        )

        self.store.commit(
            updated,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        stored = self.store.records("works")[0]
        self.assertEqual(stored["work_id"], original_work_id)
        self.assertEqual(stored["doi"], "10.1000/late-doi")
        self.assertEqual(len(self.store.records("work_aliases")), 1)

    def test_late_edn_requires_verified_alias(self) -> None:
        """Поздний EDN должен иметь отдельное проверенное происхождение."""

        self.store.commit(self.plan())
        updated = self.plan()
        work = updated.works[0]
        work["edn"] = "LATEEDN"
        work["work_aliases"].append("edn:LATEEDN")
        work["updated_at"] = "2026-08-28T12:00:00+03:00"
        updated.work_update_reasons[work["work_id"]] = "Найден EDN."

        with self.assertRaises(ManifestConflictError):
            self.store.preflight(
                [updated],
                expected_snapshot_hashes=self.store.snapshot_hashes(),
            )

        retrieval_id = updated.artifacts[0]["retrievals"][0]["retrieval_id"]
        updated.work_aliases.append(
            {
                "schema_version": "work-aliases-v1",
                "alias_record_id": "alias:test:late-edn",
                "created_at": "2026-08-28T12:00:00+03:00",
                "work_id": work["work_id"],
                "alias_type": "edn",
                "alias_value": "LATEEDN",
                "verified_at": "2026-08-28T12:00:00+03:00",
                "evidence_sha256": "c" * 64,
                "source_retrieval_id": retrieval_id,
                "supersedes_alias_record_id": None,
            }
        )

        self.store.commit(
            updated,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        self.assertEqual(self.store.records("works")[0]["edn"], "LATEEDN")

    def test_pending_identity_conflict_quarantines_work(self) -> None:
        """Открытый конфликт должен сохранять прежнее поле и включать карантин."""

        self.store.commit(self.plan())
        updated = self.plan()
        work = updated.works[0]
        work["eligibility_status"] = "quarantined"
        work["exclusion_reason"] = "Открытый конфликт названия."
        work["updated_at"] = "2026-08-28T12:00:00+03:00"
        updated.work_update_reasons[work["work_id"]] = "Обнаружен конфликт."
        retrieval_id = updated.artifacts[0]["retrievals"][0]["retrieval_id"]
        updated.identity_conflicts.append(
            {
                "schema_version": "identity-conflicts-v1",
                "conflict_id": "conflict:test:title",
                "created_at": "2026-08-28T12:00:00+03:00",
                "work_id": work["work_id"],
                "field": "title",
                "existing_value": work["title"],
                "candidate_value": "Противоречащее название",
                "source_retrieval_ids": [retrieval_id],
                "status": "pending",
                "resolution_reason": None,
                "resolved_at": None,
            }
        )

        self.store.commit(
            updated,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        stored = self.store.records("works")[0]
        self.assertEqual(stored["eligibility_status"], "quarantined")
        self.assertEqual(stored["title"], "Квантовые свойства плазмы")

    def test_retrieval_response_blob_can_exist_without_artifact(self) -> None:
        """Сырой ответ RSS может принадлежать событию без artifact-записи."""

        body = b"<rss version='2.0'></rss>"
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.xml"
        acquisition = self.right("acquisition")
        storage = self.right("storage")
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:rss-response",
            "created_at": TEST_TIMESTAMP,
            "request_context_type": "source",
            "request_context_id": self.profile.source_id,
            "source_group_id": self.profile.source_group_id,
            "requested_url": "https://example.invalid/feed.xml",
            "final_url": "https://example.invalid/feed.xml",
            "retrieved_at": TEST_TIMESTAMP,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [
                acquisition["rights_record_id"],
                storage["rights_record_id"],
            ],
            "http_status": 200,
            "response_headers": {"content-type": "application/rss+xml"},
            "response_metadata_sha256": "d" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }
        plan = ManifestPlan(
            rights=[acquisition, storage],
            retrieval_events=[event],
            blobs=[PlannedBlob(path, body, digest)],
        )

        result = self.store.commit(plan)

        self.assertEqual(result.inserted["retrieval_events"], 1)
        self.assertEqual((self.project_root / path).read_bytes(), body)
        retrieval_decisions = [
            item
            for item in self.store.records("operation_decisions")
            if item["subject_type"] == "retrieval"
            and item["subject_id"] == event["retrieval_id"]
        ]
        self.assertEqual(
            {item["operation"] for item in retrieval_decisions},
            {"acquisition", "storage"},
        )
        self.assertTrue(
            all(item["status"] == "allowed" for item in retrieval_decisions)
        )

    def test_source_event_accepts_source_group_rights(self) -> None:
        """Событие источника должно наследовать права его группы."""

        body = b"<rss version='2.0'></rss>"
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.xml"
        acquisition = self.right("acquisition")
        storage = self.right("storage")

        for right in (acquisition, storage):
            right["scope_type"] = "source_group"
            right["scope_id"] = self.profile.source_group_id

        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:source-group-rights",
            "created_at": TEST_TIMESTAMP,
            "request_context_type": "source",
            "request_context_id": self.profile.source_id,
            "source_group_id": self.profile.source_group_id,
            "requested_url": "https://example.invalid/feed.xml",
            "final_url": "https://example.invalid/feed.xml",
            "retrieved_at": TEST_TIMESTAMP,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [
                acquisition["rights_record_id"],
                storage["rights_record_id"],
            ],
            "http_status": 200,
            "response_headers": {"content-type": "application/rss+xml"},
            "response_metadata_sha256": "d" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }
        plan = ManifestPlan(
            rights=[acquisition, storage],
            retrieval_events=[event],
            blobs=[PlannedBlob(path, body, digest)],
        )

        result = self.store.commit(plan)

        self.assertEqual(result.inserted["retrieval_events"], 1)
        self.assertTrue(self.store.audit().ok)

    def test_late_storage_prohibition_blocks_retained_response(self) -> None:
        """Поздний запрет хранения должен учитывать ответ без артефакта."""

        body = b"<rss version='2.0'></rss>"
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.xml"
        acquisition = self.right("acquisition")
        storage = self.right("storage")
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:retained-response",
            "created_at": TEST_TIMESTAMP,
            "request_context_type": "source",
            "request_context_id": self.profile.source_id,
            "source_group_id": self.profile.source_group_id,
            "requested_url": "https://example.invalid/feed.xml",
            "final_url": "https://example.invalid/feed.xml",
            "retrieved_at": TEST_TIMESTAMP,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [
                acquisition["rights_record_id"],
                storage["rights_record_id"],
            ],
            "http_status": 200,
            "response_headers": {"content-type": "application/rss+xml"},
            "response_metadata_sha256": "d" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }
        self.store.commit(
            ManifestPlan(
                rights=[acquisition, storage],
                retrieval_events=[event],
                blobs=[PlannedBlob(path, body, digest)],
            )
        )
        prohibited = self.right("storage", status="prohibited")
        prohibited.update(
            {
                "created_at": "2026-08-28T12:00:00+03:00",
                "rights_checked_at": "2026-08-28",
                "rights_record_id": "right-storage-later-blocked",
                "access_basis": "Поздний запрет хранения ответа.",
            }
        )

        with self.assertRaises(ManifestError):
            self.store.commit(ManifestPlan(rights=[prohibited]))

        rights_ids = {
            item["rights_record_id"] for item in self.store.records("rights")
        }
        self.assertNotIn(prohibited["rights_record_id"], rights_ids)
        self.assertEqual((self.project_root / path).read_bytes(), body)
        self.assertTrue(self.store.audit().ok)

    def test_commit_rolls_back_registries_after_snapshot_failure(self) -> None:
        """Ошибка второго снимка не должна оставлять журналы и первый снимок."""

        original_replace = ManifestStore._atomic_replace_snapshot
        plan = self.plan()
        blob_path = self.project_root / plan.blobs[0].relative_path

        def fail_on_artifacts(
            path: Path,
            kind: str,
            records: dict[str, dict[str, Any]],
        ) -> None:
            """Имитировать сбой после успешной записи works."""

            if kind == "artifacts":
                raise OSError("Синтетический сбой записи artifacts")

            original_replace(path, kind, records)

        with mock.patch.object(
            ManifestStore,
            "_atomic_replace_snapshot",
            side_effect=fail_on_artifacts,
        ):
            with self.assertRaises(OSError):
                self.store.commit(plan)

        for kind in (
            "works",
            "artifacts",
            "rights",
            "work_revisions",
            "artifact_revisions",
            "retrieval_events",
            "operation_decisions",
        ):
            self.assertEqual(self.store.records(kind), [])

        self.assertFalse(blob_path.exists())

    def test_artifact_bytes_are_immutable_after_materialization(self) -> None:
        """Новые байты должны получать новую запись артефакта."""

        self.store.commit(self.plan())
        updated = self.plan()
        artifact = updated.artifacts[0]
        artifact["sha256"] = "f" * 64
        artifact["artifact_id"] = f"sha256:{artifact['sha256']}"
        artifact["updated_at"] = "2026-08-28T12:00:00+03:00"
        updated.artifact_update_reasons[
            artifact["artifact_record_id"]
        ] = "Попытка заменить байты."

        with self.assertRaises(ManifestConflictError):
            self.store.preflight(
                [updated],
                expected_snapshot_hashes=self.store.snapshot_hashes(),
            )

    def test_rollback_keeps_preexisting_blob(self) -> None:
        """Откат не должен удалять совпадающий blob, существовавший до commit."""

        plan = self.plan()
        blob = plan.blobs[0]
        blob_path = self.project_root / blob.relative_path
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(blob.data)

        with mock.patch.object(
            ManifestStore,
            "_atomic_replace_snapshot",
            side_effect=OSError("Синтетический сбой снимка"),
        ):
            with self.assertRaises(OSError):
                self.store.commit(plan)

        self.assertEqual(blob_path.read_bytes(), blob.data)

    def test_artifact_event_must_be_materialized_in_retrievals(self) -> None:
        """Событие контекста artifact должно появиться в текущем снимке."""

        plan = self.plan()
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:not-materialized",
            "created_at": TEST_TIMESTAMP,
            "request_context_type": "artifact",
            "request_context_id": plan.artifacts[0]["artifact_record_id"],
            "source_group_id": None,
            "requested_url": "https://example.invalid/second",
            "final_url": None,
            "retrieved_at": TEST_TIMESTAMP,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": ["right-acquisition"],
            "http_status": None,
            "response_headers": {},
            "response_metadata_sha256": "e" * 64,
            "response_path": None,
            "response_sha256": None,
            "response_bytes": None,
            "outcome": "metadata_only",
            "error_code": None,
            "error_detail": None,
        }
        plan.retrieval_events.append(event)

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_successful_retrieval_requires_permitting_acquisition_right(self) -> None:
        """Запрещающее право не должно разрешать успешное получение ответа."""

        body = b"response"
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.bin"
        acquisition = self.right("acquisition", status="prohibited")
        storage = self.right("storage")
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:test:prohibited",
            "created_at": TEST_TIMESTAMP,
            "request_context_type": "source",
            "request_context_id": self.profile.source_id,
            "source_group_id": self.profile.source_group_id,
            "requested_url": "https://example.invalid/response",
            "final_url": "https://example.invalid/response",
            "retrieved_at": TEST_TIMESTAMP,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [
                acquisition["rights_record_id"],
                storage["rights_record_id"],
            ],
            "http_status": 200,
            "response_headers": {},
            "response_metadata_sha256": "f" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }

        with self.assertRaises(ManifestError):
            self.store.preflight(
                [
                    ManifestPlan(
                        rights=[acquisition, storage],
                        retrieval_events=[event],
                        blobs=[PlannedBlob(path, body, digest)],
                    )
                ]
            )

    def test_future_condition_fulfilment_is_rejected(self) -> None:
        """Будущее выполнение условия не должно разрешать операцию сейчас."""

        conditional = self.right("acquisition", status="conditional")
        condition = "Получить согласование."
        conditional["rights_conditions"] = [condition]
        plan = self.plan(rights=[conditional, self.right("storage")])
        plan.condition_fulfilments.append(
            {
                "schema_version": "condition-fulfilments-v1",
                "fulfilment_id": "fulfilment:test:future",
                "created_at": "2099-01-01T00:00:00+00:00",
                "rights_record_id": conditional["rights_record_id"],
                "condition": condition,
                "subject_type": "project",
                "subject_id": "ruphysbert",
                "status": "satisfied",
                "satisfied_at": "2099-01-01T00:00:00+00:00",
                "expires_at": None,
                "evidence_sha256": "a" * 64,
                "supersedes_fulfilment_id": None,
            }
        )

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_manual_allowed_decision_must_match_controlling_right(self) -> None:
        """Ручной allowed нельзя обосновать правом на другую операцию."""

        plan = self.plan()
        storage = next(
            right for right in plan.rights if right["operation"] == "storage"
        )
        context = {
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
        }
        plan.operation_decisions.append(
            {
                "schema_version": "operation-decisions-v1",
                "decision_id": "decision:test:forged",
                "created_at": TEST_TIMESTAMP,
                "decision_key": "forged:artifact:acquisition",
                "operation": "acquisition",
                "derivative_scope": None,
                "subject_type": "artifact",
                "subject_id": plan.artifacts[0]["artifact_record_id"],
                "decision_at": TEST_TIMESTAMP,
                "context": context,
                "context_sha256": sha256_bytes(
                    canonical_json(context).encode("utf-8")
                ),
                "rights_record_ids": [storage["rights_record_id"]],
                "rights_snapshot_sha256": sha256_bytes(
                    f"{canonical_json(storage)}\n".encode("utf-8")
                ),
                "condition_fulfilment_ids": [],
                "supersedes_decision_id": None,
                "status": "allowed",
            }
        )

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_pending_identity_conflict_can_be_resolved_append_only(self) -> None:
        """Новое решение должно закрывать прежнюю pending-запись."""

        self.store.commit(self.plan())
        pending_plan = self.plan()
        pending_work = pending_plan.works[0]
        pending_work["eligibility_status"] = "quarantined"
        pending_work["exclusion_reason"] = "Открытый конфликт названия."
        pending_work["updated_at"] = "2026-08-28T12:00:00+03:00"
        pending_plan.work_update_reasons[pending_work["work_id"]] = "Конфликт."
        retrieval_id = pending_plan.artifacts[0]["retrievals"][0]["retrieval_id"]
        pending_plan.identity_conflicts.append(
            {
                "schema_version": "identity-conflicts-v1",
                "conflict_id": "conflict:test:pending-title",
                "created_at": "2026-08-28T12:00:00+03:00",
                "work_id": pending_work["work_id"],
                "field": "title",
                "existing_value": pending_work["title"],
                "candidate_value": "Уточнённое название",
                "source_retrieval_ids": [retrieval_id],
                "status": "pending",
                "resolution_reason": None,
                "resolved_at": None,
            }
        )
        self.store.commit(
            pending_plan,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        resolved_plan = self.plan()
        resolved_work = resolved_plan.works[0]
        resolved_work["title"] = "Уточнённое название"
        resolved_work["eligibility_status"] = "pending"
        resolved_work["exclusion_reason"] = None
        resolved_work["updated_at"] = "2026-08-29T12:00:00+03:00"
        resolved_plan.work_update_reasons[
            resolved_work["work_id"]
        ] = "Конфликт разрешён экспертом."
        resolved_plan.identity_conflicts.append(
            {
                "schema_version": "identity-conflicts-v1",
                "conflict_id": "conflict:test:resolved-title",
                "created_at": "2026-08-29T12:00:00+03:00",
                "work_id": resolved_work["work_id"],
                "field": "title",
                "existing_value": "Квантовые свойства плазмы",
                "candidate_value": "Уточнённое название",
                "source_retrieval_ids": [retrieval_id],
                "status": "resolved_replace_current",
                "resolution_reason": "Проверена карточка издателя.",
                "resolved_at": "2026-08-29T12:00:00+03:00",
            }
        )
        self.store.commit(
            resolved_plan,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        self.assertEqual(
            self.store.records("works")[0]["title"],
            "Уточнённое название",
        )
        self.assertTrue(self.store.audit().ok)

    def test_revision_order_uses_absolute_time(self) -> None:
        """Разные UTC-offset должны сортироваться по абсолютному времени."""

        self.store.commit(self.plan())
        updated = self.plan()
        work = updated.works[0]
        work["abstract"] = "Добавленная аннотация."
        work["updated_at"] = "2026-08-27T09:30:00+00:00"
        updated.work_update_reasons[work["work_id"]] = "Добавлена аннотация."

        self.store.commit(
            updated,
            expected_snapshot_hashes=self.store.snapshot_hashes(),
        )

        self.assertTrue(self.store.audit().ok)

    def test_freeze_detects_unlisted_file(self) -> None:
        """Лишний файл должен нарушать неизменяемость каталога фиксации."""

        self.store.commit(self.plan())
        result = self.store.freeze("corpus-extra-file", self.store.snapshot_hashes())
        (result.path / "unexpected.txt").write_text("changed", encoding="utf-8")

        self.assertFalse(self.store.verify_frozen("corpus-extra-file").ok)

    def test_rights_supersedes_cannot_cross_derivative_scope(self) -> None:
        """Решение о метриках не должно замещать решение о выпуске набора."""

        previous = self.right("acquisition")
        previous.update(
            {
                "rights_record_id": "right-release-metrics",
                "operation": "derivatives_release",
                "acquisition_method": None,
                "acquisition_scope": None,
                "derivative_scope": ["aggregate_metrics"],
                "created_at": "2026-08-27T11:00:00+03:00",
            }
        )
        successor = copy.deepcopy(previous)
        successor.update(
            {
                "rights_record_id": "right-release-dataset",
                "derivative_scope": ["dataset"],
                "created_at": TEST_TIMESTAMP,
                "supersedes_rights_record_id": previous["rights_record_id"],
            }
        )

        with self.assertRaises(ManifestError):
            self.store.preflight([ManifestPlan(rights=[previous, successor])])

    def test_freeze_is_immutable_and_detects_tampering(self) -> None:
        """Фиксация должна быть повторяемой и обнаруживать изменение файлов."""

        self.store.commit(self.plan())
        result = self.store.freeze("corpus-test-v1", self.store.snapshot_hashes())

        self.assertTrue(self.store.verify_frozen("corpus-test-v1").ok)

        with self.assertRaises(ManifestConflictError):
            self.store.freeze("corpus-test-v1", self.store.snapshot_hashes())

        (result.path / "works.jsonl").write_bytes(b"changed\n")
        self.assertFalse(self.store.verify_frozen("corpus-test-v1").ok)


if __name__ == "__main__":
    unittest.main()
