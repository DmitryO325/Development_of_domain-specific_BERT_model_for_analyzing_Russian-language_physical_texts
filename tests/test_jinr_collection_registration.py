"""Проверки отдельной офлайн-регистрации полнотекстовых материалов сборников ОИЯИ."""

from __future__ import annotations

import copy
import tempfile
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.jinr_collection_registration import (
    plan_collection_batch,
    reconcile_collection_plan,
)
from src.corpus.manifests import ManifestConflictError, ManifestError, ManifestPlan, ManifestStore
from src.corpus.registration import RegistrationOptions
from src.corpus.schema_validation import SchemaCatalog
from tests.jinr_collection_fixtures import CollectionFixture

ROOT = Path(__file__).resolve().parents[1]
COLLECTED_AT = "2026-10-06T12:00:00+00:00"
LATER_COLLECTED_AT = "2026-10-07T12:00:00+00:00"


class JinrCollectionRegistrationTests(unittest.TestCase):
    """Проверки метаданных, происхождения, прав и повторной безопасной записи."""

    def setUp(self) -> None:
        """Создать отдельный проект с проверенной работой сборника и двумя копиями PDF."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.fixture = CollectionFixture(self.root, aliases=2)
        self.options = RegistrationOptions(
            content_role="full_text", acquisition_method="manual_download",
            acquisition_scope="bulk",
            rights_record_ids=("right-manual_download", "right-storage"),
            extraction_method="not_started", extraction_version="jinr-collection-pdf-v1",
            response_representation="pdf", request_context_type="work",
        )

    def _plan(self, collected_at: str = COLLECTED_AT) -> ManifestPlan:
        """Передать раздельные явные права на PDF и сохранённые метаданные API."""

        return plan_collection_batch(
            self.root, self.fixture.path, self.options,
            collected_at=collected_at,
            metadata_rights_record_ids=("right-api", "right-storage"),
        )

    def _store(self) -> ManifestStore:
        """Открыть реестры только временного проекта с действующими схемами."""

        return ManifestStore(
            project_root=self.root, manifest_dir=self.root / "manifests",
            schema_dir=ROOT / "manifests/schemas",
        )

    def _snapshot(self) -> dict[str, bytes]:
        """Снять содержимое файлов для проверки отсутствия побочных изменений."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def _right(self, operation: str) -> dict[str, Any]:
        """Подготовить исключительно синтетическое разрешение для интеграционного теста."""

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
        """Добавить проверяемые тестовые права, которые регистратор сам не назначает."""

        plan = self._plan()
        plan.rights = [self._right(operation) for operation in ("api", "manual_download", "storage")]

        return plan

    def test_work_keeps_collection_separate_from_journal(self) -> None:
        """Название сборника и исходный Book chapter не подменяются журнальной статьёй."""

        plan = self._plan()
        work = plan.works[0]

        self.assertEqual(work["source_id"], "F04_JINR_COLLECTIONS_RU")
        self.assertEqual(work["source_group_id"], "F04_JINR_REPOSITORY")
        self.assertEqual(work["genre"], "collection_paper")
        self.assertEqual(work["source_document_type"], "Book chapter")
        self.assertEqual(work["collection_title"], self.fixture.item["metadata"]["dc.relation.ispartof"][0]["value"])
        self.assertIsNone(work["journal_id"])
        self.assertIsNone(work["journal_title"])
        self.assertEqual(work["published_year"], 2024)
        self.assertIsNone(work["published_at"])
        self.assertEqual(work["eligibility_status"], "pending")
        self.assertEqual(plan.rights, [])
        self.assertEqual(plan.operation_decisions, [])

    def test_no_doi_keeps_source_native_identity_and_optional_edn(self) -> None:
        """Отсутствующий DOI не выдумывается: сохраняются UUID источника и однозначный EDN."""

        self.fixture.item["metadata"].pop("dc.identifier.doi", None)
        self.fixture.item["metadata"]["local.EDN"] = [{"value": "abcdef"}]
        self.fixture.save_metadata()
        plan = self._plan()

        self.assertIsNone(plan.works[0]["doi"])
        self.assertEqual(plan.works[0]["edn"], "ABCDEF")
        self.assertEqual(
            {alias["alias_type"] for alias in plan.work_aliases},
            {"source_native_id", "edn"},
        )
        self.assertEqual(
            [alias["alias_value"] for alias in plan.work_aliases if alias["alias_type"] == "source_native_id"],
            [f"F04_JINR_COLLECTIONS_RU:{self.fixture.item['uuid']}"],
        )

    def test_plan_keeps_original_provenance_and_two_manual_histories(self) -> None:
        """Один PDF сохраняет обе истории ручного получения и отдельный контекст API."""

        plan = self._plan()
        metadata_events = [event for event in plan.retrieval_events if event["acquisition_method"] == "api"]
        manual_events = [event for event in plan.retrieval_events if event["acquisition_method"] == "manual_download"]

        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(manual_events), 2)
        self.assertEqual(len(plan.artifacts[0]["retrievals"]), 2)
        self.assertEqual(plan.artifacts[0]["qa_status"], "not_evaluated")
        self.assertEqual(len(metadata_events), 1)
        self.assertEqual(metadata_events[0]["request_context_id"], "F04_JINR_COLLECTIONS_RU")
        self.assertEqual(metadata_events[0]["source_group_id"], "F04_JINR_REPOSITORY")

        for event in manual_events:
            self.assertIsNone(event["http_status"])
            self.assertIsNone(event["final_url"])
            self.assertEqual(event["request_context_id"], plan.works[0]["work_id"])

    def test_plan_is_offline_read_only_and_schema_valid(self) -> None:
        """Подготовка ничего не пишет, не использует сеть и удовлетворяет схемам."""

        before = self._snapshot()

        with patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена")):
            plan = self._plan()

        self.assertEqual(self._snapshot(), before)
        catalog = SchemaCatalog(ROOT / "manifests/schemas")

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in getattr(plan, kind):
                catalog.validate(kind, record)

    def test_preflight_requires_applicable_rights_without_partial_write(self) -> None:
        """Знание ID прав не заменяет действующих разрешений на API и PDF."""

        store = self._store()
        plan = self._plan()
        before = self._snapshot()

        with self.assertRaises(ManifestError):
            store.preflight([plan])

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(store.records("works"), [])

    def test_commit_and_later_repeat_are_idempotent(self) -> None:
        """Предварительная проверка и повтор позднее не меняют байты зарегистрированного пакета."""

        store = self._store()
        plan = self._plan_with_rights()
        before = self._snapshot()
        store.preflight([copy.deepcopy(plan)])
        self.assertEqual(self._snapshot(), before)
        store.commit(plan)
        committed = self._snapshot()
        repeated, expected_hashes = reconcile_collection_plan(store, self._plan(LATER_COLLECTED_AT))
        result = store.commit(repeated, expected_snapshot_hashes=expected_hashes)

        self.assertEqual(sum(result.inserted.values()), 0)
        self.assertEqual(sum(result.updated.values()), 0)
        self.assertEqual(self._snapshot(), committed)
        self.assertTrue(store.audit().ok)

    def test_collection_title_conflict_stops_reconciliation(self) -> None:
        """Изменение непустого названия сборника не перезаписывает подтверждённую работу."""

        store = self._store()
        store.commit(self._plan_with_rights())
        candidate = self._plan(LATER_COLLECTED_AT)
        candidate.works[0]["collection_title"] = "Другой научный сборник"
        before = self._snapshot()

        with self.assertRaisesRegex(ManifestConflictError, "конфликт"):
            reconcile_collection_plan(store, candidate)

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(store.records("identity_conflicts"), [])

    def test_direct_collection_title_replacement_is_rejected(self) -> None:
        """Прямая запись не обходит защиту названия сборника без разрешённого конфликта."""

        store = self._store()
        store.commit(self._plan_with_rights())
        candidate = self._plan(LATER_COLLECTED_AT)
        candidate.works[0]["collection_title"] = "Другой научный сборник"
        before = self._snapshot()

        with self.assertRaises(ManifestError):
            store.commit(candidate)

        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
