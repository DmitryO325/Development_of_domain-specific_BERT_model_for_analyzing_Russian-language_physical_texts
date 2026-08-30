"""Модульные тесты хранилища реестров корпуса."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.collect.base import Document
from src.corpus.manifests import (
    ManifestConflictError,
    ManifestError,
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
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
        self.assertEqual(second.inserted, {"rights": 0, "works": 0, "artifacts": 0})
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


if __name__ == "__main__":
    unittest.main()
