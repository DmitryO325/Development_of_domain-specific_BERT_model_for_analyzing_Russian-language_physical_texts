"""Проверки отдельного хранения локального TXT без вымышленного скачивания."""

from __future__ import annotations

import copy
import tempfile
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.extraction_registration import plan_extracted_text
from src.corpus.journal_registration import plan_collected_article
from src.corpus.manifests import (
    ManifestError, ManifestPlan, ManifestStore, PRIMARY_FIELDS, REGISTRY_FILES,
    _BlobSpec, sha256_bytes,
)
from tests.test_journal_registration import JournalFixture

REGISTERED_AT = "2026-10-13T12:02:00+00:00"
STORAGE_AT = "2026-10-14T10:00:00+00:00"
EXTRACTED_AT = "2026-10-14T11:00:00+00:00"


class DerivedOperationDecisionTests(unittest.TestCase):
    """Сохранять происхождение PDF и проверять узкое право TXT независимо от acquisition."""

    def setUp(self) -> None:
        """Зарегистрировать только синтетические HTML и PDF с точечными правами."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = JournalFixture(Path(temporary.name))
        self.store = self.fixture.store()
        plan = plan_collected_article(
            self.fixture.article(), registered_at=REGISTERED_AT, scoped_to_response=True,
            rights_record_ids=("html-acquisition", "html-storage"),
            pdf_rights_record_ids=("pdf-acquisition", "pdf-storage"),
        )

        for artifact in plan.artifacts:
            for right in self.fixture.rights():
                right["scope_type"] = "artifact"
                right["scope_id"] = artifact["artifact_record_id"]
                right["rights_record_id"] = f"{artifact['representation']}-{right['operation']}"
                plan.rights.append(right)

        self.store.commit(plan)
        self.parent = next(row for row in self.store.records("artifacts") if row["representation"] == "pdf")
        self.html = next(row for row in self.store.records("artifacts") if row["representation"] == "html")

    def _text_plan(self, *, historical_acquisition: bool = False) -> ManifestPlan:
        """Создать синтетический текст и точечное право хранения позже скачивания PDF."""

        right_ids = ("text-storage", "text-acquisition") if historical_acquisition else ("text-storage",)
        plan = plan_extracted_text(
            self.parent, "Синтетическое описание квантовых состояний.",
            extraction_method="pdf", extraction_version="derived-decisions-test-v1",
            extracted_at=EXTRACTED_AT, rights_record_ids=right_ids,
        )
        child = plan.artifacts[0]
        storage = copy.deepcopy(self.fixture.rights()[1])
        storage.update(
            scope_type="artifact", scope_id=child["artifact_record_id"], rights_record_id="text-storage",
            created_at=STORAGE_AT, rights_checked_at="2026-10-14",
        )
        plan.rights.append(storage)

        if historical_acquisition:
            # Исключительно синтетическая история старого алгоритма, не основание реальных загрузок.
            acquisition = copy.deepcopy(self.fixture.rights()[0])
            acquisition.update(
                scope_type="artifact", scope_id=child["artifact_record_id"], rights_record_id="text-acquisition",
            )
            plan.rights.append(acquisition)

        return plan

    def _records(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Прочитать временные реестры для изолированной проверки исторического покрытия."""

        return {
            kind: {row[PRIMARY_FIELDS[kind]]: row for row in self.store.records(kind)}
            for kind in REGISTRY_FILES
        }

    def _lineage(
        self, child: dict[str, Any], *, parent: dict[str, Any] | None = None,
        events: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        """Проверить происхождение без извлечения текста или записи файлов."""

        parent = self.parent if parent is None else parent
        events = self._records()["retrieval_events"] if events is None else events

        return self.store._is_local_pdf_text_derivative(child, {parent["artifact_id"]: parent}, events)

    def test_later_text_storage_does_not_create_new_acquisition(self) -> None:
        """Новому TXT достаточно собственного хранения; получение остаётся историей PDF."""

        before = self.store.records("retrieval_events")
        parent_before = copy.deepcopy(self.parent)
        plan = self._text_plan()
        self.store.commit(plan)
        child_id = plan.artifacts[0]["artifact_record_id"]
        decisions = [row for row in self.store.records("operation_decisions") if row["subject_id"] == child_id]

        self.assertEqual([row["operation"] for row in decisions], ["storage"])
        self.assertEqual(decisions[0]["rights_record_ids"], ["text-storage"])
        self.assertEqual(decisions[0]["status"], "allowed")
        self.assertEqual(decisions[0]["decision_at"], EXTRACTED_AT)
        self.assertEqual(self.store.records("retrieval_events"), before)
        self.assertEqual(next(row for row in self.store.records("artifacts") if row["artifact_id"] == self.parent["artifact_id"]), parent_before)
        self.assertTrue(self.store.audit().ok)

    def test_storage_remains_required(self) -> None:
        """Освобождение от нового acquisition не разрешает сохранять TXT без основания."""

        plan = self._text_plan()
        plan.rights = []
        plan.artifacts[0]["rights_record_ids"] = []

        with self.assertRaisesRegex(ManifestError, "storage"):
            self.store.preflight([plan])

    def test_parent_artifact_rights_are_not_reused_for_text(self) -> None:
        """Точные права PDF не становятся правами другой текстовой последовательности."""

        plan = self._text_plan()
        plan.artifacts[0]["rights_record_ids"] = self.parent["rights_record_ids"]

        with self.assertRaisesRegex(ManifestError, "не применимо"):
            self.store.preflight([plan])

    def test_empty_lineage_is_rejected(self) -> None:
        """Один parent_artifact_id без событий не заменяет доказанное происхождение."""

        plan = self._text_plan()
        plan.artifacts[0]["retrievals"] = []

        with self.assertRaisesRegex(ManifestError, "происхождение|состоявшегося события"):
            self.store.preflight([plan])

    def test_unrelated_successful_event_is_rejected(self) -> None:
        """Реальное получение HTML той же работы не подтверждает происхождение текста из PDF."""

        plan = self._text_plan()
        plan.artifacts[0]["retrievals"] = copy.deepcopy(self.html["retrievals"])

        with self.assertRaisesRegex(ManifestError, "подмножеством"):
            self.store.preflight([plan])

    def test_modified_projection_is_rejected(self) -> None:
        """Нельзя сохранить ID события, подменив его URL или временную метку."""

        plan = self._text_plan()
        plan.artifacts[0]["retrievals"][0]["retrieved_at"] = STORAGE_AT

        with self.assertRaisesRegex(ManifestError, "не совпадает с журналом|подмножеством"):
            self.store.preflight([plan])

    def test_duplicate_lineage_is_rejected(self) -> None:
        """Дублирование одной ссылки не допускает неоднозначного происхождения."""

        plan = self._text_plan()
        plan.artifacts[0]["retrievals"] *= 2

        with self.assertRaisesRegex(ManifestError, "повторный retrieval_id|подмножеством"):
            self.store.preflight([plan])

    def test_missing_parent_is_rejected(self) -> None:
        """Произвольный SHA не создаёт существующего PDF-родителя."""

        plan = self._text_plan()
        plan.artifacts[0]["parent_artifact_id"] = "sha256:" + "f" * 64

        with self.assertRaisesRegex(ManifestError, "родител"):
            self.store.preflight([plan])

    def test_non_pdf_parent_is_rejected(self) -> None:
        """HTML той же работы нельзя выдать за родительский PDF."""

        plan = self._text_plan()
        plan.artifacts[0]["parent_artifact_id"] = self.html["artifact_id"]
        plan.artifacts[0]["retrievals"] = copy.deepcopy(self.html["retrievals"])

        with self.assertRaisesRegex(ManifestError, "исходный PDF"):
            self.store.preflight([plan])

    def test_parent_of_another_work_is_not_exempt(self) -> None:
        """Правильный файл другой работы не позволяет обойти acquisition для ложной связи."""

        child = self._text_plan().artifacts[0]
        child["work_id"] = "another-work"
        self.assertFalse(self._lineage(child))

    def test_recorded_parent_is_not_exempt(self) -> None:
        """Инвентарный PDF остаётся заблокированным для получения производного текста."""

        child = self._text_plan().artifacts[0]
        parent = {**self.parent, "acquisition_status": "recorded"}
        self.assertFalse(self._lineage(child, parent=parent))

    def test_parent_must_be_original_and_cannot_form_cycle(self) -> None:
        """Цепочка через производный PDF и самоссылка не проходят проверку происхождения."""

        child = self._text_plan().artifacts[0]
        parent = {**self.parent, "parent_artifact_id": child["artifact_id"]}
        self.assertFalse(self._lineage(child, parent=parent))
        plan = self._text_plan()
        plan.artifacts[0]["parent_artifact_id"] = plan.artifacts[0]["artifact_id"]

        with self.assertRaisesRegex(ManifestError, "родител|цикл"):
            self.store.preflight([plan])

    def test_method_and_scope_must_match_parent(self) -> None:
        """Нельзя переименовать способ получения при создании локального текста."""

        for field, value in (("acquisition_method", "api"), ("acquisition_scope", "single")):
            with self.subTest(field=field):
                plan = self._text_plan()
                plan.artifacts[0][field] = value

                with self.assertRaisesRegex(ManifestError, "контекст локального"):
                    self.store.preflight([plan])

    def test_text_cannot_predate_successful_pdf_origin(self) -> None:
        """Дата извлечения не переносится на время до получения исходного PDF."""

        plan = self._text_plan()
        plan.artifacts[0]["created_at"] = "2026-10-12T00:00:00+00:00"

        with self.assertRaisesRegex(ManifestError, "раньше подтверждённого"):
            self.store.preflight([plan])

    def test_success_event_must_match_pdf_bytes(self) -> None:
        """Совпадения UUID события недостаточно без SHA и размера байтов родителя."""

        child = self._text_plan().artifacts[0]
        events = self._records()["retrieval_events"]
        event = events[child["retrievals"][0]["retrieval_id"]]

        for field, value in (("response_sha256", "f" * 64), ("response_bytes", 1), ("outcome", "recorded")):
            with self.subTest(field=field):
                modified_events = copy.deepcopy(events)
                modified_events[event["retrieval_id"]][field] = value
                self.assertFalse(self._lineage(child, events=modified_events))

    def test_forged_parent_projection_is_checked_against_event(self) -> None:
        """Совместная подмена проекций родителя и ребёнка не подменяет реальный журнал."""

        child = self._text_plan().artifacts[0]
        parent = copy.deepcopy(self.parent)
        parent["retrievals"][0]["retrieved_url"] = "https://example.invalid/false.pdf"
        child["retrievals"] = copy.deepcopy(parent["retrievals"])
        self.assertFalse(self._lineage(child, parent=parent))

    def test_exact_subset_survives_later_parent_retrievals(self) -> None:
        """Дополнительное получение родителя не требует переписывать старую проекцию ребёнка."""

        child = self._text_plan().artifacts[0]
        parent = copy.deepcopy(self.parent)
        parent["retrievals"].extend(copy.deepcopy(self.html["retrievals"]))
        self.assertTrue(self._lineage(child, parent=parent))

    def test_mismatched_representation_and_extraction_method_are_rejected(self) -> None:
        """Простая подстановка имени метода не превращает ошибочную запись в локальное извлечение."""

        plan = self._text_plan()
        plan.artifacts[0]["representation"] = "ocr_text"

        with self.assertRaisesRegex(ManifestError, "метод, статус"):
            self.store.preflight([plan])

    def test_original_pdf_still_requires_acquisition_decision(self) -> None:
        """Исключение распространяется на доказанный TXT, а не на исходные материалы."""

        records = self._records()
        records["operation_decisions"] = {
            key: row for key, row in records["operation_decisions"].items()
            if not (row["subject_id"] == self.parent["artifact_record_id"] and row["operation"] == "acquisition")
        }
        errors: list[str] = []
        self.store._validate_operation_decision_relations(records, errors, require_coverage=True)
        self.assertTrue(any(self.parent["artifact_record_id"] in error and "acquisition" in error for error in errors))

    def test_historical_acquisition_decisions_are_preserved(self) -> None:
        """Старые решения остаются проверяемой историей, а не удаляются миграцией."""

        plan = self._text_plan(historical_acquisition=True)

        with patch.object(ManifestStore, "_is_local_pdf_text_derivative", return_value=False):
            self.store.commit(plan)

        path = self.store.path_for("operation_decisions")
        before = path.read_bytes()
        child_id = plan.artifacts[0]["artifact_record_id"]
        historical = [row for row in self.store.records("operation_decisions") if row["subject_id"] == child_id]
        self.assertEqual({row["operation"] for row in historical}, {"acquisition", "storage"})
        self.store.preflight([])
        self.assertTrue(self.store.audit().ok)
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_historical_acquisition_is_still_rejected(self) -> None:
        """Отмена требования нового acquisition не отключает проверку старого решения."""

        plan = self._text_plan(historical_acquisition=True)

        with patch.object(ManifestStore, "_is_local_pdf_text_derivative", return_value=False):
            self.store.commit(plan)

        records = self._records()
        child_id = plan.artifacts[0]["artifact_record_id"]
        acquisition = next(row for row in records["operation_decisions"].values() if row["subject_id"] == child_id and row["operation"] == "acquisition")
        acquisition["rights_snapshot_sha256"] = "0" * 64
        errors: list[str] = []
        self.store._validate_operation_decision_relations(records, errors, require_coverage=True)
        self.assertTrue(any("rights_snapshot_sha256" in error for error in errors))

    def _evidence_payloads(
        self,
    ) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, _BlobSpec], dict[str, Any], str]:
        """Подготовить только синтетические данные для узкой проверки файлов свидетельства."""

        plan = self._text_plan()
        child = plan.artifacts[0]
        records = self._records()
        records["artifacts"][child["artifact_record_id"]] = child
        payload = b'{"synthetic_local_text_evidence": true}\n'
        digest = sha256_bytes(payload)
        right = plan.rights[0]
        right.update(
            rights_record_id=f"right:derived-text:{digest}:storage",
            rights_evidence_sha256=digest,
            basis_type="lawful_access_research_assumption",
        )
        records["rights"][right["rights_record_id"]] = right
        path_value = f"data/raw/derived_text_registration/evidence/{digest}.json"
        specs = {blob.relative_path: _BlobSpec(blob.sha256, len(blob.data)) for blob in plan.blobs}
        specs[path_value] = _BlobSpec(digest, len(payload))

        return records, specs, right, path_value

    def _check_evidence_payloads(
        self, records: dict[str, dict[str, dict[str, Any]]], specs: dict[str, _BlobSpec],
    ) -> dict[str, int]:
        """Проверить только связь байтов с артефактами, событиями и узкими свидетельствами."""

        return self.store._validate_artifact_payloads(
            records["artifacts"], specs, records["retrieval_events"], records["rights"],
        )

    def test_scoped_text_evidence_payload_is_accepted(self) -> None:
        """Свидетельство конкретного производного TXT не требует фиктивного HTTP-события."""

        records, specs, _, _ = self._evidence_payloads()
        self.assertEqual(self._check_evidence_payloads(records, specs), {"new": 2, "unchanged": 0})

    def test_text_evidence_requires_existing_exact_child_scope(self) -> None:
        """Префикс свидетельства не разрешает запись для PDF, работы или другой операции."""

        for field, value in (
            ("scope_type", "work"),
            ("scope_id", "missing-child"),
            ("scope_id", self.parent["artifact_record_id"]),
            ("operation", "acquisition"),
            ("basis_type", "explicit_license"),
        ):
            with self.subTest(field=field, value=value):
                records, specs, right, _ = self._evidence_payloads()
                right[field] = value

                with self.assertRaisesRegex(ManifestError, "точечного основания"):
                    self._check_evidence_payloads(records, specs)

    def test_text_evidence_missing_or_corrupt_file_is_rejected(self) -> None:
        """Ссылка в основании не заменяет существующий файл с точным SHA."""

        records, specs, _, path_value = self._evidence_payloads()
        del specs[path_value]

        with self.assertRaisesRegex(ManifestError, "Отсутствует свидетельство"):
            self._check_evidence_payloads(records, specs)

        target = self.fixture.root / path_value
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"corrupt")

        with self.assertRaisesRegex(ManifestError, "Повреждено свидетельство"):
            self._check_evidence_payloads(records, specs)

    def test_text_evidence_wrong_blob_sha_is_rejected(self) -> None:
        """Планируемый файл свидетельства обязан совпадать с SHA основания."""

        records, specs, _, path_value = self._evidence_payloads()
        specs[path_value] = _BlobSpec("0" * 64, 1)

        with self.assertRaisesRegex(ManifestError, "SHA свидетельства"):
            self._check_evidence_payloads(records, specs)

    def test_text_evidence_does_not_allow_unrelated_blob(self) -> None:
        """Разрешение точного пути не превращается в разрешение произвольных файлов."""

        records, specs, _, _ = self._evidence_payloads()
        specs["data/raw/derived_text_registration/evidence/extra.json"] = _BlobSpec("0" * 64, 1)

        with self.assertRaisesRegex(ManifestError, "не связан"):
            self._check_evidence_payloads(records, specs)


if __name__ == "__main__":
    unittest.main()
