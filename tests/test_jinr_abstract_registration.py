"""Проверки офлайн-регистрации тезисов без присвоения допуска или жанра полной статьи."""

from __future__ import annotations

import copy
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.jinr_abstract_registration import plan_abstract_batch, reconcile_abstract_plan
from src.corpus.manifests import ManifestConflictError, ManifestError, ManifestPlan, ManifestStore
from src.corpus.registration import RegistrationOptions
from src.corpus.schema_validation import SchemaCatalog
from tests.jinr_abstract_fixtures import AbstractFixture

ROOT = Path(__file__).resolve().parents[1]
COLLECTED_AT = "2026-10-08T12:00:00+00:00"
LATER_COLLECTED_AT = "2026-10-09T12:00:00+00:00"


class JinrAbstractRegistrationTests(unittest.TestCase):
    """Проверить метаданные, права и безопасную повторную запись разрешённых тезисов."""

    def setUp(self) -> None:
        """Создать изолированный пакет тезисов и явные параметры ручного получения."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.fixture = AbstractFixture(self.root, aliases=2)
        self.options = RegistrationOptions(
            content_role="full_text", acquisition_method="manual_download",
            acquisition_scope="bulk",
            rights_record_ids=("right-manual_download", "right-storage"),
            extraction_method="not_started", extraction_version="jinr-abstract-pdf-v1",
            response_representation="pdf", request_context_type="work",
        )

    def _plan(self, collected_at: str = COLLECTED_AT) -> ManifestPlan:
        """Подготовить план с раздельными правами на PDF и сохранённые метаданные API."""

        return plan_abstract_batch(
            self.root, self.fixture.path, self.options,
            collected_at=collected_at,
            metadata_rights_record_ids=("right-api", "right-storage"),
        )

    def _store(self) -> ManifestStore:
        """Открыть только временные реестры с действующими схемами проекта."""

        return ManifestStore(
            project_root=self.root, manifest_dir=self.root / "manifests",
            schema_dir=ROOT / "manifests/schemas",
        )

    def _snapshot(self) -> dict[str, bytes]:
        """Сохранить байты временного проекта для проверки отсутствия изменений."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def _right(self, operation: str) -> dict[str, Any]:
        """Подготовить синтетическое право на получение либо хранение только для теста."""

        acquisition = operation != "storage"

        return {
            "schema_version": "rights-v1", "created_at": "2026-09-01T00:00:00+00:00",
            "rights_record_id": f"right-{operation}", "scope_type": "source_group",
            "scope_id": "F04_JINR_REPOSITORY",
            "operation": "acquisition" if acquisition else "storage", "status": "allowed",
            "access_basis": "Исключительно синтетическое право в тесте.",
            "basis_type": "explicit_license",
            "acquisition_method": operation if acquisition else None,
            "acquisition_scope": "bulk" if acquisition else None,
            "terms_url": "https://example.invalid/license", "rights_checked_at": "2026-09-01",
            "derivative_scope": None, "rights_conditions": [], "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None, "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None, "supersedes_rights_record_id": None,
        }

    def _plan_with_rights(self) -> ManifestPlan:
        """Дополнить план синтетическими правами, которые регистратор не назначает."""

        plan = self._plan()
        plan.rights = [self._right(operation) for operation in ("api", "manual_download", "storage")]

        return plan

    def test_work_keeps_abstract_genre_container_and_pending_status(self) -> None:
        """Тезисы сохраняют Book chapter и сборник без журнала, H2 или допуска к обучению."""

        plan = self._plan()
        work = plan.works[0]

        self.assertEqual(work["source_id"], "F04_JINR_ABSTRACTS_RU")
        self.assertEqual(work["source_group_id"], "F04_JINR_REPOSITORY")
        self.assertEqual(work["genre"], "conference_abstract")
        self.assertEqual(work["source_document_type"], "Book chapter")
        self.assertEqual(work["collection_title"], self.fixture.item["metadata"]["dc.relation.ispartof"][0]["value"])
        self.assertIsNone(work["journal_id"])
        self.assertIsNone(work["journal_title"])
        self.assertEqual(work["published_year"], 2024)
        self.assertIsNone(work["published_at"])
        self.assertEqual(work["eligibility_status"], "pending")
        self.assertEqual(plan.rights, [])
        self.assertEqual(plan.operation_decisions, [])

    def test_plan_preserves_manual_and_api_provenance_without_network_or_writes(self) -> None:
        """Один PDF сохраняет обе ручные истории, исходный API и все контрольные суммы."""

        before = self._snapshot()

        with patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена")):
            plan = self._plan()

        self.assertEqual(self._snapshot(), before)
        manual = [event for event in plan.retrieval_events if event["acquisition_method"] == "manual_download"]
        metadata = [event for event in plan.retrieval_events if event["acquisition_method"] == "api"]
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(manual), 2)
        self.assertEqual(len(plan.artifacts[0]["retrievals"]), 2)
        self.assertEqual(plan.artifacts[0]["qa_status"], "not_evaluated")
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]["request_context_id"], "F04_JINR_ABSTRACTS_RU")

        for event in manual:
            self.assertIsNone(event["http_status"])
            self.assertIsNone(event["final_url"])
            self.assertEqual(event["request_context_id"], plan.works[0]["work_id"])

        catalog = SchemaCatalog(ROOT / "manifests/schemas")

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in getattr(plan, kind):
                catalog.validate(kind, record)

    def test_absent_doi_keeps_source_identity_and_optional_edn(self) -> None:
        """Без DOI сохраняются UUID источника и однозначный EDN без выдуманных значений."""

        self.fixture.item["metadata"].pop("dc.identifier.doi", None)
        self.fixture.item["metadata"]["local.EDN"] = [{"value": "abcdef"}]
        self.fixture.save_metadata()
        plan = self._plan()

        self.assertIsNone(plan.works[0]["doi"])
        self.assertEqual(plan.works[0]["edn"], "ABCDEF")
        self.assertEqual({alias["alias_type"] for alias in plan.work_aliases}, {"source_native_id", "edn"})
        self.assertEqual(
            [alias["alias_value"] for alias in plan.work_aliases if alias["alias_type"] == "source_native_id"],
            [f"F04_JINR_ABSTRACTS_RU:{self.fixture.item['uuid']}"],
        )

    def test_genre_approval_does_not_substitute_for_rights(self) -> None:
        """Согласие жанра и известные ID не разрешают получение либо хранение сами по себе."""

        store = self._store()
        plan = self._plan()
        before = self._snapshot()

        with self.assertRaises(ManifestError):
            store.preflight([plan])

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(store.records("works"), [])

    def test_commit_and_later_repeat_are_idempotent(self) -> None:
        """Повтор регистрации сохраняет байты и исторический обзор без новых записей."""

        store = self._store()
        plan = self._plan_with_rights()
        before = self._snapshot()
        store.preflight([copy.deepcopy(plan)])
        self.assertEqual(self._snapshot(), before)
        store.commit(plan)
        committed = self._snapshot()
        repeated, expected_hashes = reconcile_abstract_plan(store, self._plan(LATER_COLLECTED_AT))
        result = store.commit(repeated, expected_snapshot_hashes=expected_hashes)

        self.assertEqual(sum(result.inserted.values()), 0)
        self.assertEqual(sum(result.updated.values()), 0)
        self.assertEqual(self._snapshot(), committed)
        self.assertEqual(committed[self.fixture.review_path], before[self.fixture.review_path])
        self.assertTrue(store.audit().ok)

    def test_collection_title_conflict_stops_without_partial_write(self) -> None:
        """Непустое название сборника не заменяется при повторе регистрации тезисов."""

        store = self._store()
        store.commit(self._plan_with_rights())
        candidate = self._plan(LATER_COLLECTED_AT)
        candidate.works[0]["collection_title"] = "Другой сборник"
        before = self._snapshot()

        with self.assertRaisesRegex(ManifestConflictError, "конфликт"):
            reconcile_abstract_plan(store, candidate)

        self.assertEqual(self._snapshot(), before)

    def test_options_preserve_manual_provenance(self) -> None:
        """Нельзя приписать локальному PDF другое получение или область прав."""

        for field, value in (
            ("content_role", "title_abstract"),
            ("acquisition_method", "api"),
            ("acquisition_scope", "single"),
            ("response_representation", "json"),
            ("request_context_type", "source"),
        ):
            with self.subTest(field=field):
                options = replace(self.options, **{field: value})

                with self.assertRaisesRegex(ValueError, field):
                    plan_abstract_batch(
                        self.root, self.fixture.path, options,
                        metadata_rights_record_ids=("right-api", "right-storage"),
                    )


if __name__ == "__main__":
    unittest.main()
