"""Проверки инвентарного учёта исходных файлов без допуска к использованию."""

from __future__ import annotations

import copy
import tempfile
import unittest

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from src.collect.base import Document, HttpResponseSnapshot
from src.corpus.manifests import ManifestError, ManifestPlan, ManifestStore, sha256_bytes
from src.corpus.profiles import get_source_profile
from src.corpus.registration import (
    RegistrationOptions,
    plan_http_response,
    reconcile_document_plan,
)
from src.corpus.schema_validation import SchemaValidationError

ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_TIME = "2026-10-13T12:00:00+03:00"
REGISTRATION_TIME = "2026-10-14T12:00:00+03:00"
ARTICLE_URL = "https://ufn.ru/ru/articles/2024/1/a/"
PDF_URL = "https://ufn.ru/ufn2024/ufn2024_1/Russian/r241a.pdf"
PDF_BYTES = b"%PDF-1.7\ninventory test\n%%EOF\n"


class InventoryManifestValidationTests(unittest.TestCase):
    """Не смешивать наличие байтов с разрешением получить или использовать их."""

    def setUp(self) -> None:
        """Создать изолированное хранилище без доступа к рабочим данным."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name)
        self.store = ManifestStore(
            project_root=self.project_root,
            schema_dir=ROOT / "manifests" / "schemas",
        )
        self.profile = get_source_profile("ufn")

    def right(
        self,
        operation: str,
        *,
        status: str = "permission_required",
        method: str = "crawler",
    ) -> dict[str, Any]:
        """Описать синтетическое основание, действовавшее до скачивания."""

        return {
            "schema_version": "rights-v1",
            "created_at": "2026-10-12T12:00:00+03:00",
            "rights_record_id": f"right-{operation}-{method}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": status,
            "access_basis": "Синтетическое основание модульного теста.",
            "basis_type": "explicit_license",
            "acquisition_method": method if operation == "acquisition" else None,
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/terms",
            "rights_checked_at": "2026-10-12",
            "derivative_scope": None,
            "rights_conditions": ["Письменное согласование."] if status == "conditional" else [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def plan(
        self,
        *,
        rights: list[dict[str, Any]] | None = None,
        inventory: bool = True,
        method: str = "crawler",
        downloaded_at: str = DOWNLOAD_TIME,
    ) -> ManifestPlan:
        """Подготовить исходный PDF с точным событием и выбранными основаниями."""

        selected_rights = rights or []
        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method=method,
            acquisition_scope="sample",
            rights_record_ids=tuple(right["rights_record_id"] for right in selected_rights),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
            inventory_only=inventory,
        )
        document = Document(
            source="ufn.ru",
            url=ARTICLE_URL,
            title="Квантовые свойства плазмы",
            text="",
            pdf_url=PDF_URL,
        )
        response = HttpResponseSnapshot(
            requested_url=PDF_URL,
            final_url=PDF_URL,
            status_code=200,
            headers=(("content-type", "application/pdf"),),
            retrieved_at=downloaded_at,
            body=PDF_BYTES,
        )
        plan = plan_http_response(
            document, self.profile, options, REGISTRATION_TIME, response,
        )
        plan.rights = selected_rights

        return plan

    def test_empty_rights_create_unknown_decisions_without_permission(self) -> None:
        """Отсутствующие основания не превращаются в разрешение через all([])."""

        plan = self.plan()
        self.store.commit(plan)
        decisions = self.store.records("operation_decisions")

        self.assertEqual(len(decisions), 4)
        self.assertEqual({item["status"] for item in decisions}, {"unknown"})
        self.assertTrue(all(item["rights_record_ids"] == [] for item in decisions))
        self.assertTrue(all(item["rights_snapshot_sha256"] == sha256_bytes(b"") for item in decisions))
        self.assertEqual(self.store.records("rights"), [])
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "quarantined")
        self.assertEqual(self.store.records("artifacts")[0]["qa_status"], "quarantined")
        self.assertTrue(self.store.audit().ok, self.store.audit().errors)
        repeated = self.store.commit(plan)
        self.assertEqual(sum(repeated.inserted.values()), 0)
        self.assertEqual(sum(repeated.updated.values()), 0)

    def test_blocked_and_conditional_rights_remain_unfulfilled(self) -> None:
        """Учёт сохраняет запрет получения и невыполненные условия хранения."""

        plan = self.plan(rights=[self.right("acquisition"), self.right("storage", status="conditional")])
        self.store.commit(plan)
        decisions = self.store.records("operation_decisions")

        self.assertEqual({item["status"] for item in decisions if item["operation"] == "acquisition"}, {"blocked"})
        self.assertEqual({item["status"] for item in decisions if item["operation"] == "storage"}, {"conditional_pending"})
        self.assertEqual(self.store.records("condition_fulfilments"), [])
        self.assertTrue(self.store.audit().ok, self.store.audit().errors)

    def test_missing_controlling_reference_is_rejected(self) -> None:
        """Пустой список не скрывает существующее определяющее основание."""

        for kind in ("artifacts", "retrieval_events"):
            for operation in ("acquisition", "storage"):
                with self.subTest(kind=kind, operation=operation):
                    plan = self.plan(rights=[self.right(operation)])
                    getattr(plan, kind)[0]["rights_record_ids"] = []

                    with self.assertRaisesRegex(ManifestError, "определяющ"):
                        self.store.preflight([plan])

    def test_postdated_right_does_not_backdate_acquisition_permission(self) -> None:
        """Позднее основание не меняет unknown исторического получения."""

        right = self.right("acquisition", status="allowed")
        right["created_at"] = REGISTRATION_TIME
        right["rights_checked_at"] = "2026-10-14"
        self.store.commit(self.plan(rights=[right]))
        acquisition = [item for item in self.store.records("operation_decisions") if item["operation"] == "acquisition"]

        self.assertTrue(all(item["status"] == "unknown" for item in acquisition))
        self.assertTrue(all(datetime.fromisoformat(item["decision_at"]) == datetime.fromisoformat(DOWNLOAD_TIME) for item in acquisition))

    def test_normal_retrieved_and_succeeded_still_require_permission(self) -> None:
        """Инвентарный режим не ослабляет обычный допуск исходного ответа."""

        plan = self.plan(
            inventory=False,
            rights=[self.right("acquisition"), self.right("storage")],
        )

        with self.assertRaisesRegex(ManifestError, "не разрешена|не имеет разрешающего|не разрешено"):
            self.store.preflight([plan])

    def test_normal_event_cannot_have_empty_rights(self) -> None:
        """Исключение из minItems действует исключительно для recorded."""

        event = self.plan().retrieval_events[0]
        event["outcome"] = "succeeded"

        with self.assertRaises(SchemaValidationError):
            self.store.schemas.validate("retrieval_events", event)

    def test_recorded_cannot_be_processed_eligible_or_derived(self) -> None:
        """Строгая схема оставляет только исходный необработанный карантин."""

        changes: dict[str, Any] = {
            "qa_status": "passed",
            "extraction_status": "succeeded",
            "processing_status": "eligible",
            "parent_artifact_id": "sha256:" + "a" * 64,
            "extraction_method": "ocr",
            "ocr_method": "tesseract",
            "characters": 1,
            "h2_input_sha256": "a" * 64,
            "path": None,
            "bytes": None,
            "retrievals": [],
        }

        for field, value in changes.items():
            with self.subTest(field=field):
                artifact = self.plan().artifacts[0]
                artifact[field] = value

                with self.assertRaises(SchemaValidationError):
                    self.store.schemas.validate("artifacts", artifact)

    def test_derived_artifact_cannot_use_recorded_parent(self) -> None:
        """Даже отдельно разрешённый дочерний объект не обходит карантин родителя."""

        plan = self.plan(rights=[self.right("acquisition", status="allowed"), self.right("storage", status="allowed")])
        child = copy.deepcopy(plan.artifacts[0])
        child["artifact_record_id"] = "artifact:child"
        child["artifact_id"] = "sha256:" + "b" * 64
        child["sha256"] = "b" * 64
        child["parent_artifact_id"] = plan.artifacts[0]["artifact_id"]
        child["path"] = None
        child["bytes"] = None
        child["acquisition_status"] = "retrieved"
        event = copy.deepcopy(plan.retrieval_events[0])
        event["retrieval_id"] = "retrieval:child"
        event["outcome"] = "succeeded"
        child["retrievals"][0]["retrieval_id"] = event["retrieval_id"]
        plan.artifacts.append(child)
        plan.retrieval_events.append(event)

        with self.assertRaisesRegex(ManifestError, "производные от recorded"):
            self.store.preflight([plan])

    def test_recorded_event_is_not_proof_for_retrieved_artifact(self) -> None:
        """Переключение статуса не превращает заблокированное получение в разрешённое."""

        plan = self.plan(rights=[self.right("acquisition", status="allowed"), self.right("storage", status="allowed")])
        plan.artifacts[0]["acquisition_status"] = "retrieved"

        with self.assertRaisesRegex(ManifestError, "не имеет состоявшегося"):
            self.store.preflight([plan])

    def test_recorded_origin_must_match_bytes_context_and_method(self) -> None:
        """Запись о другом файле, работе или способе получения не подтверждает файл."""

        changes: dict[str, Any] = {
            "response_sha256": "b" * 64,
            "response_bytes": len(PDF_BYTES) + 1,
            "response_path": "data/raw/another.pdf",
            "acquisition_method": "api",
            "acquisition_scope": "bulk",
            "request_context_id": "other-work",
        }

        for field, value in changes.items():
            with self.subTest(field=field):
                plan = self.plan()
                plan.retrieval_events[0][field] = value

                with self.assertRaisesRegex(ManifestError, "recorded требует"):
                    self.store.preflight([plan])

    def test_inventory_retains_timestamp_validation(self) -> None:
        """Регистрация инвентаря не может предшествовать самому получению."""

        plan = self.plan()
        plan.retrieval_events[0]["created_at"] = "2026-10-12T12:00:00+03:00"

        with self.assertRaisesRegex(ManifestError, "created_at|knowledge_cutoff_at"):
            self.store.preflight([plan])

    def test_inventory_retains_payload_hash_validation(self) -> None:
        """Карантинный статус не позволяет записать несовпадающие байты."""

        plan = self.plan()
        plan.blobs[0] = replace(plan.blobs[0], data=PDF_BYTES + b"changed")

        with self.assertRaisesRegex(ManifestError, "хеш|SHA|sha256"):
            self.store.preflight([plan])

    def test_inventory_retains_http_success_validation(self) -> None:
        """Инвентарный файл не маскирует ответ HTTP 403 под полученный документ."""

        event = self.plan().retrieval_events[0]
        event["http_status"] = 403

        with self.assertRaises(SchemaValidationError):
            self.store.schemas.validate("retrieval_events", event)

    def test_unknown_decision_cannot_forge_status_or_empty_snapshot(self) -> None:
        """Историческое решение проверяет статус, контекст и SHA даже без прав."""

        self.store.commit(self.plan())
        records = {
            "works": {item["work_id"]: item for item in self.store.records("works")},
            "artifacts": {item["artifact_record_id"]: item for item in self.store.records("artifacts")},
            "retrieval_events": {item["retrieval_id"]: item for item in self.store.records("retrieval_events")},
            "rights": {},
            "condition_fulfilments": {},
            "operation_decisions": {},
        }
        original = self.store.records("operation_decisions")[0]

        for field, value in (("status", "allowed"), ("rights_snapshot_sha256", "c" * 64), ("context_sha256", "d" * 64)):
            with self.subTest(field=field):
                decision = {**original, field: value}
                records["operation_decisions"] = {decision["decision_id"]: decision}
                errors: list[str] = []
                self.store._validate_operation_decision_relations(records, errors, require_coverage=False)
                self.assertTrue(errors)

    def test_manual_artifact_keeps_permission_with_recorded_crawler_event(self) -> None:
        """Новая ссылка на те же байты не понижает ранее разрешённый ручной файл."""

        initial = self.plan(
            inventory=False,
            method="manual_download",
            rights=[self.right("acquisition", status="allowed", method="manual_download"), self.right("storage", status="allowed")],
        )
        self.store.commit(initial)
        previous = self.store.records("artifacts")[0]
        plan = self.plan(rights=[self.right("acquisition"), self.right("storage", status="allowed")])
        reconciled, hashes = reconcile_document_plan(self.store, plan)
        self.store.commit(reconciled, expected_snapshot_hashes=hashes)
        artifact = self.store.records("artifacts")[0]

        self.assertEqual(artifact["acquisition_status"], "retrieved")
        self.assertEqual(artifact["acquisition_method"], "manual_download")
        self.assertEqual(artifact["rights_record_ids"], previous["rights_record_ids"])
        self.assertEqual(len(artifact["retrievals"]), 2)
        self.assertEqual({item["outcome"] for item in self.store.records("retrieval_events")}, {"succeeded", "recorded"})
        self.assertTrue(self.store.audit().ok, self.store.audit().errors)


if __name__ == "__main__":
    unittest.main()
