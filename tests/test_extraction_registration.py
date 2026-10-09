"""Проверки регистрации текста, извлечённого из локального PDF."""

from __future__ import annotations

import copy
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any

from src.corpus.extraction_registration import (
    _merge_retrievals,
    find_registered_pdf_artifact,
    plan_extracted_text,
)
from src.corpus.local_registration import LocalFileRegistration, plan_local_file
from src.corpus.manifests import (
    ManifestConflictError,
    ManifestPlan,
    ManifestStore,
)
from src.corpus.profiles import get_source_profile
from src.corpus.registration import RegistrationOptions

ROOT = Path(__file__).resolve().parents[1]
PDF_BYTES = b"%PDF-1.7\nregistered extraction test\n%%EOF\n"
REGISTERED_AT = "2024-01-10T12:30:00+03:00"
EXTRACTED_AT = "2024-01-10T13:00:00+03:00"


class ExtractedTextRegistrationTests(unittest.TestCase):
    """Проверки происхождения и идемпотентности производного текста."""

    def setUp(self) -> None:
        """Создать временный проект с зарегистрированным PDF и правами."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.pdf_path = self.project_root / "data" / "raw" / "manual" / "r241a.pdf"
        self.pdf_path.parent.mkdir(parents=True)
        self.pdf_path.write_bytes(PDF_BYTES)
        self.profile = get_source_profile("ufn")
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )
        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )
        plan = plan_local_file(
            self.registration(),
            self.profile,
            options,
            project_root=self.project_root,
        )
        plan.rights = [self.right("acquisition"), self.right("storage")]
        self.store.commit(plan)
        self.parent_artifact = find_registered_pdf_artifact(
            self.store,
            "data/raw/manual/r241a.pdf",
            project_root=self.project_root,
        )

    def tearDown(self) -> None:
        """Удалить временный проект после проверки."""

        self.temporary_directory.cleanup()

    def registration(self) -> LocalFileRegistration:
        """Создать корректную карточку исходного PDF."""

        return LocalFileRegistration(
            relative_path="data/raw/manual/r241a.pdf",
            source_url="https://ufn.ru/ufn2024/ufn2024_1/Russian/r241a.pdf",
            canonical_url="https://ufn.ru/ru/articles/2024/1/a/",
            retrieved_at=REGISTERED_AT,
            title="Квантовые свойства плазмы",
            authors=["Иванов И. И."],
            doi="10.1000/phys.1",
            published_at="2024-01-10",
            section="Обзоры актуальных проблем",
            language="ru",
            genre="review_article",
            abstract="Аннотация физической статьи.",
            keywords=["плазма"],
            pacs_codes_raw=[],
            udc_codes_raw=[],
            acquisition_agent="Ручная проверка",
            eligibility_status="pending",
            exclusion_reason=None,
        )

    def right(self, operation: str) -> dict[str, Any]:
        """Создать разрешающую запись права для временного источника."""

        return {
            "schema_version": "rights-v1",
            "created_at": "2024-01-09T10:00:00+03:00",
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": "allowed",
            "access_basis": "Синтетическое разрешение для теста.",
            "basis_type": "explicit_license",
            "acquisition_method": (
                "manual_download" if operation == "acquisition" else None
            ),
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2024-01-09",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def test_plain_text_is_registered_as_child_without_new_retrieval(self) -> None:
        """Чистый текст должен стать дочерним артефактом исходного PDF."""

        text = "Квантовая плазма описывается физической моделью."
        plan = plan_extracted_text(
            self.parent_artifact,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )

        self.assertEqual(plan.retrieval_events, [])
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(plan.blobs), 1)

        text_artifact = plan.artifacts[0]
        self.assertEqual(
            text_artifact["parent_artifact_id"],
            self.parent_artifact["artifact_id"],
        )
        self.assertEqual(text_artifact["representation"], "plain_text")
        self.assertEqual(text_artifact["qa_status"], "not_evaluated")
        self.assertEqual(
            text_artifact["rights_record_ids"],
            ["right-acquisition", "right-storage"],
        )

        self.store.commit(plan)
        output_path = self.project_root / text_artifact["path"]

        self.assertEqual(output_path.read_text(encoding="utf-8"), text)
        self.assertEqual(len(self.store.records("retrieval_events")), 1)
        self.assertEqual(len(self.store.records("artifacts")), 2)
        self.assertTrue(self.store.audit().ok)

    def test_ocr_metadata_records_engine_version(self) -> None:
        """OCR-текст должен хранить метод, движок и точную версию."""

        plan = plan_extracted_text(
            self.parent_artifact,
            "Распознанный русский физический текст достаточной длины.",
            extraction_method="pdf_ocr_layout",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
            ocr_version="5.5.3",
        )
        artifact = plan.artifacts[0]

        self.assertEqual(artifact["representation"], "ocr_text")
        self.assertEqual(artifact["ocr_method"], "tesseract")
        self.assertEqual(artifact["ocr_version"], "5.5.3")

        self.store.preflight([plan])

    def test_repeated_plan_reuses_compatible_artifact(self) -> None:
        """Повторное извлечение тех же байтов не должно создавать ревизию."""

        text = "Одинаковый результат извлечения русского текста."
        first_plan = plan_extracted_text(
            self.parent_artifact,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )
        self.store.commit(first_plan)
        second_plan = plan_extracted_text(
            self.parent_artifact,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at="2024-01-10T14:00:00+03:00",
            existing_artifacts=self.store.records("artifacts"),
        )
        result = self.store.commit(second_plan)

        self.assertEqual(result.unchanged["artifacts"], 1)
        self.assertEqual(result.unchanged_blobs, 1)
        self.assertEqual(len(self.store.records("artifacts")), 2)

    def test_reused_artifact_inherits_new_parent_provenance(self) -> None:
        """Повтор должен дополнить получения и права из родительского PDF."""

        text = "Одинаковый результат с дополненным происхождением."
        first_plan = plan_extracted_text(
            self.parent_artifact,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )
        self.store.commit(first_plan)
        updated_parent = copy.deepcopy(self.parent_artifact)
        updated_parent["retrievals"].append(
            {
                "retrieval_id": "retrieval:new-parent-copy",
                "retrieved_url": "https://ufn.ru/second-copy.pdf",
                "retrieved_at": "2024-01-10T13:30:00+03:00",
                "response_metadata_sha256": "b" * 64,
            }
        )
        updated_parent["rights_record_ids"].append("right-new-storage")
        second_plan = plan_extracted_text(
            updated_parent,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at="2024-01-10T14:00:00+03:00",
            existing_artifacts=self.store.records("artifacts"),
        )
        artifact = second_plan.artifacts[0]

        self.assertEqual(len(artifact["retrievals"]), 2)
        self.assertIn("right-new-storage", artifact["rights_record_ids"])
        self.assertIn(
            artifact["artifact_record_id"],
            second_plan.artifact_update_reasons,
        )

    def test_repeated_text_preserves_reverse_retrieval_order_without_revisions(self) -> None:
        """Перестановка двух известных получений не меняет текст, реестры или историю."""

        second_retrieved_at = "2024-01-10T12:45:00+03:00"
        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )
        second_download = plan_local_file(
            replace(self.registration(), retrieved_at=second_retrieved_at),
            self.profile,
            options,
            project_root=self.project_root,
        )
        parent = copy.deepcopy(self.parent_artifact)
        parent["retrievals"].extend(second_download.artifacts[0]["retrievals"])
        parent["retrievals"].sort(key=lambda record: record["retrieval_id"], reverse=True)
        parent["updated_at"] = second_retrieved_at
        parent_plan = ManifestPlan(
            artifacts=[parent],
            retrieval_events=second_download.retrieval_events,
            artifact_update_reasons={
                parent["artifact_record_id"]: "Добавлено второе получение того же PDF.",
            },
        )
        self.store.commit(parent_plan, expected_snapshot_hashes=self.store.snapshot_hashes())

        self.assertEqual(len(self.store.records("retrieval_events")), 2)
        self.assertEqual(len(parent["retrievals"]), 2)
        self.assertGreater(parent["retrievals"][0]["retrieval_id"], parent["retrievals"][1]["retrieval_id"])

        text = "Одинаковый текст PDF с двумя подтверждёнными событиями получения."
        first_plan = plan_extracted_text(
            parent,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )
        self.store.commit(first_plan)
        committed_bytes = {
            path.relative_to(self.project_root).as_posix(): path.read_bytes()
            for path in self.project_root.rglob("*")
            if path.is_file()
        }
        committed_artifacts = self.store.records("artifacts")
        committed_revisions = self.store.records("artifact_revisions")
        committed_decisions = self.store.records("operation_decisions")
        repeated_parent = copy.deepcopy(parent)
        repeated_parent["retrievals"].reverse()
        repeated_plan = plan_extracted_text(
            repeated_parent,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at="2024-01-10T14:00:00+03:00",
            existing_artifacts=committed_artifacts,
        )
        expected_hashes = self.store.snapshot_hashes()
        preview = self.store.commit(
            repeated_plan, dry_run=True, expected_snapshot_hashes=expected_hashes,
        )
        result = self.store.commit(repeated_plan, expected_snapshot_hashes=expected_hashes)

        self.assertEqual(result.updated["artifacts"], 0)
        self.assertEqual(sum(preview.inserted.values()), 0)
        self.assertEqual(sum(preview.updated.values()), 0)
        self.assertEqual(sum(result.inserted.values()), 0)
        self.assertEqual(sum(result.updated.values()), 0)
        self.assertEqual(result.written_blobs, 0)
        self.assertEqual(result.unchanged_blobs, 1)
        self.assertEqual(
            {
                path.relative_to(self.project_root).as_posix(): path.read_bytes()
                for path in self.project_root.rglob("*")
                if path.is_file()
            },
            committed_bytes,
        )
        self.assertEqual(self.store.records("artifacts"), committed_artifacts)
        self.assertEqual(self.store.records("artifact_revisions"), committed_revisions)
        self.assertEqual(self.store.records("operation_decisions"), committed_decisions)
        self.assertEqual(len(self.store.records("retrieval_events")), 2)
        self.assertTrue(self.store.audit().ok)

    def test_same_version_rejects_competing_text_for_one_pdf(self) -> None:
        """Одна версия не должна давать два текста одного исходного PDF."""

        first_plan = plan_extracted_text(
            self.parent_artifact,
            "Первый результат извлечения.",
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )
        self.store.commit(first_plan)

        with self.assertRaisesRegex(ManifestConflictError, "той же версии"):
            plan_extracted_text(
                self.parent_artifact,
                "Другой результат извлечения.",
                extraction_method="pdf_ocr_layout",
                extraction_version="pdf-text-test-v1",
                extracted_at="2024-01-10T14:00:00+03:00",
                existing_artifacts=self.store.records("artifacts"),
                ocr_version="5.5.3",
            )

    def test_same_text_with_different_provenance_is_rejected(self) -> None:
        """Одинаковые байты не должны молча терять версию происхождения."""

        text = "Одинаковый текст с несовместимым способом извлечения."
        first_plan = plan_extracted_text(
            self.parent_artifact,
            text,
            extraction_method="pdf",
            extraction_version="pdf-text-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )
        self.store.commit(first_plan)

        with self.assertRaisesRegex(ManifestConflictError, "происхождением"):
            plan_extracted_text(
                self.parent_artifact,
                text,
                extraction_method="pdf_ocr_layout",
                extraction_version="pdf-text-test-v2",
                extracted_at="2024-01-10T14:00:00+03:00",
                existing_artifacts=self.store.records("artifacts"),
                ocr_version="5.5.3",
            )

    def test_tampered_pdf_is_rejected_before_extraction(self) -> None:
        """Изменённые после регистрации байты PDF нельзя обрабатывать."""

        self.pdf_path.write_bytes(PDF_BYTES + b"changed")

        with self.assertRaisesRegex(ManifestConflictError, "SHA-256"):
            find_registered_pdf_artifact(
                self.store,
                "data/raw/manual/r241a.pdf",
                project_root=self.project_root,
            )

    def test_path_outside_data_is_rejected(self) -> None:
        """Карточка не должна читать файл за пределами каталога data/."""

        outside_path = self.project_root / "outside.pdf"
        outside_path.write_bytes(PDF_BYTES)

        with self.assertRaisesRegex(ValueError, "внутри data"):
            find_registered_pdf_artifact(
                self.store,
                "outside.pdf",
                project_root=self.project_root,
            )


class TextRetrievalMergeTests(unittest.TestCase):
    """Проверки устойчивого порядка и конфликтов происхождения текста."""

    def test_empty_candidate_preserves_existing_order(self) -> None:
        """Отсутствие новых событий не переставляет уже сохранённые."""

        existing = [{"retrieval_id": "retrieval-z"}, {"retrieval_id": "retrieval-a"}]

        self.assertEqual(_merge_retrievals(existing, []), existing)

    def test_new_events_append_once_without_changing_inputs(self) -> None:
        """Новые события дописываются однократно, вложенные значения копируются."""

        existing = [{"retrieval_id": "retrieval-z", "details": {"value": "old"}}]
        first_new = {"retrieval_id": "retrieval-b", "details": {"value": "first"}}
        second_new = {"retrieval_id": "retrieval-a", "details": {"value": "second"}}
        candidate = [first_new, existing[0], second_new, first_new]
        original_existing = copy.deepcopy(existing)
        original_candidate = copy.deepcopy(candidate)
        result = _merge_retrievals(existing, candidate)

        self.assertEqual(result, [existing[0], first_new, second_new])

        for record in result:
            record["details"]["value"] = "changed"

        self.assertEqual(existing, original_existing)
        self.assertEqual(candidate, original_candidate)

    def test_conflicting_ids_are_rejected_without_changing_inputs(self) -> None:
        """Разные данные одного ID вызывают ошибку и среди прежних, и среди новых событий."""

        original = {"retrieval_id": "retrieval-a", "details": {"value": "original"}}
        conflicting = {"retrieval_id": "retrieval-a", "details": {"value": "conflict"}}

        for existing, candidate in (
            ([original], [conflicting]),
            ([], [original, conflicting]),
        ):
            with self.subTest(existing=existing):
                original_existing = copy.deepcopy(existing)
                original_candidate = copy.deepcopy(candidate)

                with self.assertRaisesRegex(ManifestConflictError, "имеет разные проекции"):
                    _merge_retrievals(existing, candidate)

                self.assertEqual(existing, original_existing)
                self.assertEqual(candidate, original_candidate)


if __name__ == "__main__":
    unittest.main()
