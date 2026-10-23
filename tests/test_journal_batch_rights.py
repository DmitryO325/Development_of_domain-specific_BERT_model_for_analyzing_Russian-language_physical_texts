"""Проверки точных областей действия и честного времени оснований партии журналов."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any

from src.collect.base import Document, HttpResponseSnapshot
from src.corpus.journal_batch_rights import (
    COMMITMENTS,
    attach_batch_rights,
    batch_response_rights_ids,
)
from src.corpus.manifests import (
    ManifestError,
    ManifestPlan,
    ManifestStore,
    PlannedBlob,
    sha256_bytes,
)
from src.corpus.profiles import get_source_profile
from src.corpus.registration import (
    RegistrationOptions,
    plan_http_response,
    reconcile_document_plan,
    resolve_collection_rights,
)
from src.corpus.schema_validation import SchemaCatalog

ROOT = Path(__file__).resolve().parents[1]
BATCH_ID = "synthetic-journal-batch-v1"
RETRIEVED_AT = "2026-10-12T10:00:00.000000+00:00"
REGISTERED_AT = "2026-10-14T10:00:00.000000+00:00"
ARTICLE_URL = "https://www.jetp.ras.ru/cgi-bin/r/index/r/170/2/p300?a=list"


class JournalBatchRightsTests(unittest.TestCase):
    """Проверки условного закрытого хранения без разрешения будущего сбора или ML."""

    def setUp(self) -> None:
        """Подготовить изолированный реестр и синтетическую HTML-карточку ЖЭТФ."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name)
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )
        self.catalog = SchemaCatalog(ROOT / "manifests" / "schemas")
        self.profile = get_source_profile("jetp")
        self.response = HttpResponseSnapshot(
            requested_url=ARTICLE_URL,
            final_url=ARTICLE_URL,
            status_code=200,
            headers=(("content-type", "text/html; charset=utf-8"),),
            retrieved_at=RETRIEVED_AT,
            body=b"<html>synthetic physics article</html>",
        )
        self.document = Document(
            source="jetp",
            url=ARTICLE_URL,
            title="Исследование плазмы",
            text="Аннотация физической статьи",
            extra={"article_id": "article123"},
        )

    def evidence(self, response: HttpResponseSnapshot | None = None) -> dict[str, Any]:
        """Подготовить явно синтетическое подтверждение всех условий партии."""

        response = response or self.response

        return {
            "policy": {
                "decision_id": "DEC-012",
                "adopted_at": "2026-08-19T00:00:00+03:00",
                "snapshot": "Синтетический снимок принятой политики DEC-012.",
            },
            "prepared_batch_sha256": "a" * 64,
            "commitments": {name: True for name in COMMITMENTS},
            "reviewed_responses": [
                {
                    "response_metadata_sha256": response.metadata_sha256(),
                    "response_sha256": sha256_bytes(response.body),
                }
            ],
        }

    def plan(self, *, context: str = "artifact") -> ManifestPlan:
        """Подготовить ответ с заранее определёнными узкими ID оснований."""

        options = RegistrationOptions(
            content_role="metadata_only",
            acquisition_method="crawler",
            acquisition_scope="bulk",
            rights_record_ids=batch_response_rights_ids(BATCH_ID, self.response),
            extraction_method="not_started",
            extraction_version="synthetic-batch-v1",
            response_representation="html",
            request_context_type=context,
        )

        return plan_http_response(
            self.document, self.profile, options, REGISTERED_AT, self.response,
        )

    def attach(self, plan: ManifestPlan | None = None, **changes: Any) -> ManifestPlan:
        """Применить тестовую оценку к плану, не меняя исходные записи."""

        arguments: dict[str, Any] = {
            "batch_id": BATCH_ID,
            "registered_at": REGISTERED_AT,
            "evidence": self.evidence(),
        }
        arguments.update(changes)

        return attach_batch_rights(plan or self.plan(), **arguments)

    def test_artifact_rights_have_narrow_scope_and_honest_timestamps(self) -> None:
        """Дата записи отличается от фактического получения без расширения области."""

        original = self.plan()
        result = self.attach(original)
        artifact_id = result.artifacts[0]["artifact_record_id"]
        self.assertEqual(original.rights, [])
        self.assertEqual(original.condition_fulfilments, [])
        self.assertEqual(result.works, original.works)
        self.assertEqual(result.artifacts, original.artifacts)
        self.assertEqual(len(result.rights), 2)
        self.assertEqual(len(result.condition_fulfilments), 4)

        for right in result.rights:
            self.catalog.validate("rights", right)
            self.assertEqual(right["scope_type"], "artifact")
            self.assertEqual(right["scope_id"], artifact_id)
            self.assertEqual(right["status"], "conditional")
            self.assertEqual(right["basis_type"], "lawful_access_research_assumption")
            self.assertEqual(right["created_at"], REGISTERED_AT)
            self.assertEqual(right["effective_from"], RETRIEVED_AT)
            self.assertIsNone(right["conditions_satisfied_at"])

        for fulfilment in result.condition_fulfilments:
            self.catalog.validate("condition_fulfilments", fulfilment)
            self.assertEqual(fulfilment["subject_type"], "artifact")
            self.assertEqual(fulfilment["subject_id"], artifact_id)
            self.assertEqual(fulfilment["created_at"], REGISTERED_AT)
            self.assertEqual(fulfilment["satisfied_at"], RETRIEVED_AT)

        self.assertEqual(
            {right["operation"] for right in result.rights}, {"acquisition", "storage"},
        )

    def test_evidence_is_immutable_and_keeps_exact_response_pair(self) -> None:
        """Неизменяемое свидетельство связывает политику, проверку и точные ответы."""

        result = self.attach()
        blob = result.blobs[-1]
        self.assertEqual(blob.sha256, sha256_bytes(blob.data))
        self.assertEqual(blob.relative_path, f"data/raw/journal_registration/evidence/{blob.sha256}.json")
        record = json.loads(blob.data)
        self.assertEqual(record["assessment"], self.evidence())
        self.assertEqual(record["responses"][0]["response_metadata_sha256"], self.response.metadata_sha256())
        self.assertEqual(record["responses"][0]["response_sha256"], sha256_bytes(self.response.body))
        self.assertEqual(record["responses"][0]["retrieved_at"], RETRIEVED_AT)
        self.assertEqual(result.rights[0]["rights_evidence_sha256"], blob.sha256)

    def test_commit_repeat_and_audit_leave_no_training_permission(self) -> None:
        """Повторная атомарная регистрация не создаёт новые записи или допуск ML."""

        result = self.attach()
        first = self.store.commit(result)
        reconciled, snapshot = reconcile_document_plan(self.store, result)
        second = self.store.commit(reconciled, expected_snapshot_hashes=snapshot)
        self.assertEqual(first.inserted["rights"], 2)
        self.assertEqual(sum(second.inserted.values()), 0)
        self.assertEqual(sum(second.updated.values()), 0)
        self.assertEqual(second.written_blobs, 0)
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "pending")
        self.assertTrue(self.store.audit().ok)
        self.assertEqual(
            {right["operation"] for right in self.store.records("rights")},
            {"acquisition", "storage"},
        )

    def test_scoped_rights_do_not_authorize_new_collection(self) -> None:
        """Основания старой партии не могут разрешить следующий обход сайта."""

        self.store.commit(self.attach())

        with self.assertRaises(ManifestError):
            resolve_collection_rights(
                self.store,
                self.profile,
                acquisition_method="crawler",
                acquisition_scope="bulk",
                allowed_rights_record_ids=batch_response_rights_ids(BATCH_ID, self.response),
            )

    def test_audit_detects_missing_evidence_file(self) -> None:
        """Утрата свидетельства должна быть видна даже при целых статьях."""

        plan = self.attach()
        self.store.commit(plan)
        evidence_path = self.project_root / plan.blobs[-1].relative_path
        evidence_path.unlink()
        report = self.store.audit()
        self.assertFalse(report.ok)
        self.assertTrue(any("Отсутствует свидетельство партии" in error for error in report.errors))

    def test_audit_detects_corrupted_evidence_file(self) -> None:
        """Замена байтов свидетельства не должна проходить по прежнему SHA."""

        plan = self.attach()
        self.store.commit(plan)
        evidence_path = self.project_root / plan.blobs[-1].relative_path
        evidence_path.write_bytes(b"corrupted synthetic evidence")
        report = self.store.audit()
        self.assertFalse(report.ok)
        self.assertTrue(any("Повреждено свидетельство партии" in error for error in report.errors))

    def test_unreferenced_blob_remains_rejected(self) -> None:
        """Поддержка свидетельств не открывает запись произвольных посторонних файлов."""

        plan = self.attach()
        data = b"unreferenced synthetic evidence"
        digest = sha256_bytes(data)
        plan.blobs.append(
            PlannedBlob(f"data/raw/journal_registration/evidence/{digest}.json", data, digest)
        )

        with self.assertRaisesRegex(ManifestError, "не связан"):
            self.store.commit(plan)

        self.assertEqual(self.store.records("rights"), [])

    def test_article_cannot_occupy_evidence_path_with_different_bytes(self) -> None:
        """Существующий путь статьи не должен скрывать несовпадение SHA свидетельства."""

        plan = self.attach()
        evidence_path = plan.blobs[-1].relative_path
        article_blob = plan.blobs[0]
        plan.artifacts[0]["path"] = evidence_path
        plan.retrieval_events[0]["response_path"] = evidence_path
        plan.blobs = [PlannedBlob(evidence_path, article_blob.data, article_blob.sha256)]

        with self.assertRaises(ManifestError):
            self.store.commit(plan)

    def test_shared_toc_uses_only_retrieval_scope(self) -> None:
        """Общее оглавление не становится разрешением на весь источник."""

        result = self.attach(self.plan(context="source"))
        retrieval_id = result.retrieval_events[0]["retrieval_id"]
        self.assertEqual(result.artifacts, [])

        for right in result.rights:
            self.assertEqual(right["scope_type"], "retrieval")
            self.assertEqual(right["scope_id"], retrieval_id)

        for fulfilment in result.condition_fulfilments:
            self.assertEqual(fulfilment["subject_type"], "retrieval")
            self.assertEqual(fulfilment["subject_id"], retrieval_id)

        self.store.commit(result)
        self.assertTrue(self.store.audit().ok)

    def test_identifiers_depend_on_batch_and_bytes_not_registration_time(self) -> None:
        """Новая партия, другой ответ или другое тело требуют других оснований."""

        identifiers = batch_response_rights_ids(BATCH_ID, self.response)
        self.assertEqual(identifiers, batch_response_rights_ids(BATCH_ID, self.response))
        self.assertNotEqual(identifiers, batch_response_rights_ids("batch2", self.response))
        self.assertNotEqual(
            identifiers, batch_response_rights_ids(BATCH_ID, replace(self.response, body=b"other")),
        )
        self.assertNotEqual(
            identifiers,
            batch_response_rights_ids(BATCH_ID, replace(self.response, retrieved_at=REGISTERED_AT)),
        )

    def test_unreviewed_bytes_or_metadata_are_rejected(self) -> None:
        """Один и тот же адрес не позволяет применить оценку к другим байтам."""

        for field in ("response_sha256", "response_metadata_sha256"):
            with self.subTest(field=field):
                evidence = self.evidence()
                evidence["reviewed_responses"][0][field] = "b" * 64

                with self.assertRaisesRegex(ValueError, "перечне проверенных"):
                    self.attach(evidence=evidence)

    def test_each_commitment_is_required_explicitly(self) -> None:
        """Ни одно условие нельзя незаметно принять за пользователя."""

        for name in COMMITMENTS:
            with self.subTest(name=name):
                evidence = self.evidence()
                evidence["commitments"].pop(name)

                with self.assertRaisesRegex(ValueError, "условия"):
                    self.attach(evidence=evidence)

    def test_newer_policy_or_missing_timezone_is_rejected(self) -> None:
        """Нельзя выдавать последующее решение за действовавшее при скачивании."""

        for timestamp in (REGISTERED_AT, "2026-08-19T00:00:00"):
            with self.subTest(timestamp=timestamp):
                evidence = self.evidence()
                evidence["policy"]["adopted_at"] = timestamp

                with self.assertRaises(ValueError):
                    self.attach(evidence=evidence)

    def test_event_registration_time_must_not_be_backdated(self) -> None:
        """Историческое retrieved_at не должно заменять текущую дату регистрации."""

        plan = self.plan()
        plan.retrieval_events[0]["created_at"] = RETRIEVED_AT

        with self.assertRaisesRegex(ValueError, "Время регистрации"):
            self.attach(plan)

    def test_work_context_and_external_rights_are_rejected(self) -> None:
        """Основания не должны расширять область до всей работы или чужих прав."""

        with self.assertRaisesRegex(ValueError, "узкое событие"):
            self.attach(self.plan(context="work"))

        plan = self.plan()
        plan.retrieval_events[0]["rights_record_ids"].append("unrelated-right")

        with self.assertRaisesRegex(ValueError, "два заранее"):
            self.attach(plan)

    def test_existing_rights_or_conflicts_are_rejected(self) -> None:
        """План с решениями или конфликтами нельзя автоматически одобрить."""

        for name in ("rights", "condition_fulfilments", "operation_decisions", "identity_conflicts"):
            with self.subTest(name=name):
                plan = self.plan()
                getattr(plan, name).append({"synthetic": True})

                with self.assertRaises(ValueError):
                    self.attach(plan)

    def test_quantum_pdf_is_rejected_even_with_positive_assessment(self) -> None:
        """Утверждённая проверка HTML не должна разрешать подписной PDF."""

        plan = self.plan()
        plan.works[0]["source_group_id"] = "S06_QUANTUM_ELECTRONICS"
        plan.artifacts[0]["representation"] = "pdf"
        plan.artifacts[0]["content_role"] = "full_text"

        with self.assertRaisesRegex(ValueError, "только HTML"):
            self.attach(plan)

    def test_unknown_source_or_non_html_source_response_is_rejected(self) -> None:
        """Проверка не распространяется на другие журналы и общие бинарные файлы."""

        plan = self.plan()
        plan.works[0]["source_group_id"] = "S01_UFN"

        with self.assertRaisesRegex(ValueError, "Источник"):
            self.attach(plan)

        plan = self.plan(context="source")
        plan.retrieval_events[0]["response_headers"]["content-type"] = "application/pdf"

        with self.assertRaisesRegex(ValueError, "HTML-оглавлением"):
            self.attach(plan)

    def test_repeated_attachment_is_deterministic_and_does_not_mutate_evidence(self) -> None:
        """Одинаковая фиксированная дата проверки даёт побайтово одинаковый пакет."""

        evidence = self.evidence()
        before = copy.deepcopy(evidence)
        first = self.attach(evidence=evidence)
        second = self.attach(evidence=evidence)
        self.assertEqual(first, second)
        self.assertEqual(evidence, before)


if __name__ == "__main__":
    unittest.main()
