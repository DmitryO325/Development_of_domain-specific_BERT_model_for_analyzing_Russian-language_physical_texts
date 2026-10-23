"""Проверки точечных оснований хранения текстов согласованной партии."""

from __future__ import annotations

import copy
import json
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.derived_text_rights import (
    SCOPE_LIMITS,
    attach_derived_storage,
    load_derived_text_approval,
)
from src.corpus.extraction_registration import plan_extracted_text
from src.corpus.manifests import ManifestPlan, PlannedBlob, sha256_bytes

from tests import test_extraction_registration as extraction_tests

APPROVED_AT = "2024-01-10T12:40:00+03:00"
EXTRACTED_AT = "2024-01-10T13:00:00+03:00"
PARENT_EVIDENCE = b'{"synthetic_parent_evidence": true}\n'


def write_json(path: Path, record: dict[str, Any]) -> bytes:
    """Сохранить тестовое свидетельство и вернуть его действительные байты."""

    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    return payload


class DerivedTextRightsTests(unittest.TestCase):
    """Проверить состав партии, ограничения, происхождение и повторный запуск."""

    def setUp(self) -> None:
        """Создать реальный временный реестр с синтетическим зарегистрированным PDF."""

        self.fixture = extraction_tests.ExtractedTextRegistrationTests()
        original_right = self.fixture.right

        def right_with_real_evidence(operation: str) -> dict[str, Any]:
            """Заменить синтетический хеш тестового права проверяемым свидетельством."""

            right = original_right(operation)
            right["rights_evidence_sha256"] = sha256_bytes(PARENT_EVIDENCE)

            return right

        with patch.object(self.fixture, "right", side_effect=right_with_real_evidence):
            self.fixture.setUp()

        self.project_root = self.fixture.project_root
        self.store = self.fixture.store
        self.parent = self.fixture.parent_artifact
        self.parent_artifact = self.parent
        self.work = self.store.records("works")[0]
        self.package = self.project_root / "manifests" / "imports" / "derived_test"
        self.approval_path = self.package / "approval.json"
        self.evidence_path = self.project_root / "data" / "raw" / "parent_evidence.json"
        self.evidence_path.write_bytes(PARENT_EVIDENCE)
        self.candidate = {
            "format_version": "journal-extraction-candidate-v1",
            "work_id": self.parent["work_id"],
            "pdf_artifact_record_id": self.parent["artifact_record_id"],
            "pdf_artifact_id": self.parent["artifact_id"],
            "pdf_sha256": self.parent["sha256"],
            "pdf_path": self.parent["path"],
            "pdf_bytes": self.parent["bytes"],
            "source_id": self.work["source_id"],
            "genre": self.work["genre"],
            "title": self.work["title"],
            "work_eligibility_status": "pending",
            "parent_rights_record_ids": self.parent["rights_record_ids"],
        }
        candidate_bytes = write_json(self.package / "candidates.jsonl", self.candidate)
        self.summary = {
            "candidate_sha256": sha256_bytes(candidate_bytes),
            "counts": {"candidates": 1},
        }
        summary_bytes = write_json(self.package / "summary.json", self.summary)
        self.approval = {
            "format_version": "journal-derived-text-scope-approval-v1",
            "decision_id": "DEC-024",
            "policy_id": "DEC-012",
            "approved": True,
            "approved_by": "user",
            "recorded_at": APPROVED_AT,
            "scope": "closed_noncommercial_local_extraction_and_derived_text_storage",
            "basis_type": "lawful_access_research_assumption",
            "candidate_file": "manifests/imports/derived_test/candidates.jsonl",
            "candidate_sha256": sha256_bytes(candidate_bytes),
            "candidate_count": 1,
            "candidate_identity_fields": [
                "work_id",
                "pdf_artifact_record_id",
                "pdf_sha256",
            ],
            "preparation_summary": "manifests/imports/derived_test/summary.json",
            "preparation_summary_sha256": sha256_bytes(summary_bytes),
            "scope_limits": dict.fromkeys(SCOPE_LIMITS, False),
            "conditions": [
                "Закрытое некоммерческое хранение; без передачи третьим лицам."
            ],
        }
        self.review = {
            "format_version": "journal-derived-text-conditions-review-v1",
            "decision_id": "DEC-024",
            "scope": "local_saved_evidence_review_only",
            "candidate_sha256": sha256_bytes(candidate_bytes),
            "parent_pdf_count": 1,
            "parent_evidence": [
                {
                    "path": "data/raw/parent_evidence.json",
                    "sha256": sha256_bytes(PARENT_EVIDENCE),
                    "bytes": len(PARENT_EVIDENCE),
                }
            ],
        }
        self.write_approval()

    def tearDown(self) -> None:
        """Удалить только временный проект теста."""

        self.fixture.tearDown()

    def write_approval(self) -> None:
        """Обновить тестовые файлы с согласованной между ними цепочкой хешей."""

        payload = write_json(self.approval_path, self.approval)
        self.review["approval_sha256"] = sha256_bytes(payload)
        write_json(self.package / "conditions_review.json", self.review)

    def child_plan(self) -> ManifestPlan:
        """Построить отдельный TXT без автоматического переноса исходных прав."""

        plan = plan_extracted_text(
            self.parent,
            "Русский физический текст: модель квантовой плазмы.",
            extraction_method="pdf",
            extraction_version="derived-test-v1",
            extracted_at=EXTRACTED_AT,
            existing_artifacts=self.store.records("artifacts"),
        )
        plan.artifacts[0]["rights_record_ids"] = []

        return plan

    def attach(
        self, plan: ManifestPlan | None = None, *, at: str = EXTRACTED_AT
    ) -> ManifestPlan:
        """Добавить точечное хранение через публичный API модуля."""

        scope = load_derived_text_approval(
            self.approval_path, project_root=self.project_root
        )

        return attach_derived_storage(
            self.child_plan() if plan is None else plan,
            scope=scope,
            parent=self.parent,
            store=self.store,
            at=at,
        )

    def test_new_right_has_only_actual_child_storage(self) -> None:
        """Создать одно основание хранения точного TXT без получения или обучения."""

        original = self.child_plan()
        result = self.attach(original)
        right = result.rights[0]
        child = result.artifacts[0]
        self.assertEqual(original.rights, [])
        self.assertEqual(original.artifacts[0]["rights_record_ids"], [])
        self.assertEqual(len(result.rights), 1)
        self.assertEqual(right["operation"], "storage")
        self.assertEqual(right["scope_type"], "artifact")
        self.assertEqual(right["scope_id"], child["artifact_record_id"])
        self.assertNotEqual(right["scope_id"], self.parent["artifact_record_id"])
        self.assertEqual(right["status"], "conditional")
        self.assertEqual(right["created_at"], EXTRACTED_AT)
        self.assertNotIn("effective_from", right)
        self.assertIsNone(right["acquisition_method"])
        self.assertEqual(result.retrieval_events, [])
        self.assertEqual(result.works, [])
        self.assertEqual(len(result.condition_fulfilments), 2)
        self.assertTrue(
            all(
                item["satisfied_at"] == EXTRACTED_AT
                for item in result.condition_fulfilments
            )
        )
        evidence = json.loads(result.blobs[-1].data)
        self.assertTrue(any("ACL" in item for item in evidence["limitations"]))
        self.assertEqual(evidence["child_sha256"], child["sha256"])
        self.assertFalse(self.store._rights_apply(right, self.work, self.parent))
        other_child = copy.deepcopy(child)
        other_child["artifact_record_id"] = "artifact-record:sha256:" + "e" * 64
        other_child["artifact_id"] = "sha256:" + "e" * 64
        self.assertFalse(self.store._rights_apply(right, self.work, other_child))

    def test_plan_commits_and_repeat_is_idempotent(self) -> None:
        """Повтор не меняет даты основания, историю PDF и допуск работы."""

        result = self.attach()
        parent_before = copy.deepcopy(self.parent)
        works_before = self.store.records("works")
        retrievals_before = self.store.records("retrieval_events")
        self.store.commit(result)
        rights_before = self.store.records("rights")
        fulfilments_before = self.store.records("condition_fulfilments")
        repeated = self.attach(at="2024-01-11T13:00:00+03:00")
        self.assertEqual(repeated.rights, [])
        self.assertEqual(repeated.condition_fulfilments, [])
        self.store.commit(repeated)
        self.assertEqual(self.store.records("rights"), rights_before)
        self.assertEqual(
            self.store.records("condition_fulfilments"), fulfilments_before
        )
        self.assertEqual(self.store.records("works"), works_before)
        self.assertEqual(self.store.records("retrieval_events"), retrievals_before)
        self.assertIn(parent_before, self.store.records("artifacts"))
        self.assertTrue(self.store.audit().ok)

    def test_modified_candidate_bytes_fail(self) -> None:
        """Список партии нельзя дополнить после согласования даже пустой строкой."""

        candidate_path = self.package / "candidates.jsonl"
        candidate_path.write_bytes(candidate_path.read_bytes() + b"\n")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_expanded_scope_fails(self) -> None:
        """Каждый запрет в согласии обязателен и не превращается в разрешение."""

        for name in SCOPE_LIMITS:
            with self.subTest(name=name):
                self.approval["scope_limits"][name] = True
                self.write_approval()

                with self.assertRaisesRegex(ValueError, "Границы согласия"):
                    load_derived_text_approval(
                        self.approval_path, project_root=self.project_root
                    )

                self.approval["scope_limits"][name] = False

    def test_missing_scope_limit_fails(self) -> None:
        """Пропущенный запрет не считается ложным значением по умолчанию."""

        del self.approval["scope_limits"]["ml_training"]
        self.write_approval()

        with self.assertRaisesRegex(ValueError, "Границы согласия"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_duplicate_json_key_fails(self) -> None:
        """Повторное approved не может скрыть неоднозначное согласие."""

        original = self.approval_path.read_bytes()
        self.approval_path.write_bytes(b'{"approved": false, ' + original[1:])

        with self.assertRaisesRegex(ValueError, "Некорректный JSON"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_missing_candidate_title_fails(self) -> None:
        """Неполная карточка выдаёт понятную ошибку до формирования CLI-отчёта."""

        del self.candidate["title"]
        payload = write_json(self.package / "candidates.jsonl", self.candidate)
        self.approval["candidate_sha256"] = sha256_bytes(payload)
        self.write_approval()

        with self.assertRaisesRegex(ValueError, "точная идентичность PDF"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_changed_approval_after_load_fails(self) -> None:
        """Ранее загруженное согласие не используется после подмены файла."""

        scope = load_derived_text_approval(
            self.approval_path, project_root=self.project_root
        )
        self.approval_path.write_bytes(self.approval_path.read_bytes() + b"\n")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            scope.validate_parent(self.parent, store=self.store, at=EXTRACTED_AT)

    def test_mismatched_review_fails(self) -> None:
        """Обзор другой партии не подтверждает данное согласование."""

        self.review["candidate_sha256"] = "a" * 64
        self.write_approval()

        with self.assertRaisesRegex(ValueError, "Проверка условий"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_parent_evidence_tampering_fails(self) -> None:
        """Изменённые исходные свидетельства запрещают новый запуск."""

        self.evidence_path.write_bytes(b"changed")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_path_escape_fails(self) -> None:
        """Согласие не читает файлы за пределами проекта."""

        self.approval["candidate_file"] = "../candidates.jsonl"
        self.write_approval()

        with self.assertRaisesRegex(ValueError, "безопасный относительный"):
            load_derived_text_approval(
                self.approval_path, project_root=self.project_root
            )

    def test_unknown_parent_fails(self) -> None:
        """Новая загрузка не включается по совпадению источника или журнала."""

        scope = load_derived_text_approval(
            self.approval_path, project_root=self.project_root
        )
        parent = copy.deepcopy(self.parent)
        parent["artifact_record_id"] = "another-pdf"

        with self.assertRaisesRegex(ValueError, "не входит"):
            scope.validate_parent(parent, store=self.store, at=EXTRACTED_AT)

    def test_changed_pdf_bytes_fail_before_plan(self) -> None:
        """Подмена локального PDF обнаруживается до OCR и создания прав."""

        scope = load_derived_text_approval(
            self.approval_path, project_root=self.project_root
        )
        scope.validate_parent(self.parent, store=self.store, at=EXTRACTED_AT)
        (self.project_root / self.parent["path"]).write_bytes(b"changed PDF")

        with self.assertRaisesRegex(ValueError, "Байты PDF"):
            scope.validate_parent(self.parent, store=self.store, at=EXTRACTED_AT)

    def test_registry_cache_checks_changed_rights_bytes(self) -> None:
        """Кеш снимка не скрывает появившееся после предварительной проверки ограничение."""

        scope = load_derived_text_approval(
            self.approval_path, project_root=self.project_root
        )
        scope.validate_parent(self.parent, store=self.store, at=EXTRACTED_AT)
        rights = self.store.records("rights")

        for right in rights:
            if right["operation"] == "storage":
                right["status"] = "prohibited"

        payload = "".join(json.dumps(right) + "\n" for right in rights)
        self.store.path_for("rights").write_text(payload, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "ограничение"):
            scope.validate_parent(self.parent, store=self.store, at=EXTRACTED_AT)

    def test_repeated_right_does_not_restore_revoked_condition(self) -> None:
        """Повтор не создаёт новое выполнение условия вместо отозванного."""

        self.store.commit(self.attach())
        fulfilments = self.store.records("condition_fulfilments")
        original = fulfilments[-1]
        revoked = {
            **original,
            "fulfilment_id": "revoked-derived-condition",
            "status": "revoked",
            "created_at": "2024-01-11T10:00:00+03:00",
            "satisfied_at": "2024-01-11T10:00:00+03:00",
            "supersedes_fulfilment_id": original["fulfilment_id"],
        }
        fulfilments.append(revoked)
        self.store.path_for("condition_fulfilments").write_text(
            "".join(json.dumps(item) + "\n" for item in fulfilments),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "невыполненное условие"):
            self.attach(at="2024-01-11T13:00:00+03:00")

    def test_recorded_parent_fails(self) -> None:
        """Инвентарный recorded не превращается в разрешённый исходник."""

        self.parent["acquisition_status"] = "recorded"

        with self.assertRaisesRegex(ValueError, "текущей записью|recorded"):
            self.attach()

    def test_preapproval_extraction_fails(self) -> None:
        """Согласование не действует задним числом для создания TXT."""

        with self.assertRaisesRegex(ValueError, "предшествовать"):
            self.attach(at="2024-01-10T12:35:00+03:00")

    def test_parent_storage_prohibition_fails(self) -> None:
        """Существующий запрет на исходник не отменяется узким основанием TXT."""

        original_records = self.store.records

        def records_with_prohibition(kind: str) -> list[dict[str, Any]]:
            """Подставить актуальный запрет без изменения рабочего реестра теста."""

            records = original_records(kind)

            if kind == "rights":
                for right in records:
                    if right["operation"] == "storage":
                        right["status"] = "prohibited"

            return records

        with (
            patch.object(self.store, "records", side_effect=records_with_prohibition),
            self.assertRaisesRegex(ValueError, "ограничение"),
        ):
            self.attach()

    def test_unfulfilled_child_storage_condition_fails(self) -> None:
        """Новое точечное право не затеняет невыполненное условие для TXT."""

        plan = self.child_plan()
        original_records = self.store.records

        def records_with_condition(kind: str) -> list[dict[str, Any]]:
            """Добавить применимое условие для дочернего артефакта."""

            records = original_records(kind)

            if kind == "rights":
                right = copy.deepcopy(records[-1])
                right.update(
                    {
                        "rights_record_id": "child-condition",
                        "operation": "storage",
                        "scope_type": "artifact",
                        "scope_id": plan.artifacts[0]["artifact_record_id"],
                        "status": "conditional",
                        "rights_conditions": ["Не подтверждено"],
                        "acquisition_method": None,
                        "acquisition_scope": None,
                    }
                )
                records.append(right)

            return records

        with (
            patch.object(self.store, "records", side_effect=records_with_condition),
            self.assertRaisesRegex(ValueError, "невыполненное условие"),
        ):
            self.attach(plan)

    def test_changed_child_bytes_fail(self) -> None:
        """Основание относится к фактическим байтам, а не только названию файла."""

        plan = self.child_plan()
        blob = plan.blobs[0]
        plan.blobs[0] = PlannedBlob(blob.relative_path, b"changed", blob.sha256)

        with self.assertRaisesRegex(ValueError, "Байты производного"):
            self.attach(plan)

    def test_wrong_parent_link_fails(self) -> None:
        """Нельзя использовать согласие для текста другой работы или PDF."""

        plan = self.child_plan()
        plan.artifacts[0]["parent_artifact_id"] = "sha256:" + "f" * 64

        with self.assertRaisesRegex(ValueError, "данного PDF"):
            self.attach(plan)

    def test_additional_blob_fails(self) -> None:
        """Основание не покрывает дополнительные файлы, страницы или выгрузки."""

        plan = self.child_plan()
        plan.blobs.append(
            PlannedBlob("data/extracted/other.txt", b"other", sha256_bytes(b"other"))
        )

        with self.assertRaisesRegex(ValueError, "одному полному TXT"):
            self.attach(plan)

    def test_new_retrieval_in_plan_fails(self) -> None:
        """Создание локального текста не регистрирует фиктивное скачивание."""

        plan = self.child_plan()
        plan.retrieval_events.append({"unexpected": True})

        with self.assertRaisesRegex(ValueError, "посторонних реестровых"):
            self.attach(plan)

    def test_training_or_qa_change_in_plan_fails(self) -> None:
        """Точечное хранение не повышает пригодность TXT или работы."""

        plan = self.child_plan()
        plan.artifacts[0]["qa_status"] = "passed"

        with self.assertRaisesRegex(ValueError, "непроверенному TXT"):
            self.attach(plan)

    def test_previous_unapproved_derivative_fails(self) -> None:
        """Существующий чужой производный файл не включается в новую партию."""

        plan = self.child_plan()
        plan.artifacts[0]["rights_record_ids"] = self.parent["rights_record_ids"]
        self.store.commit(plan)

        with self.assertRaisesRegex(ValueError, "уже обработан"):
            self.attach()
