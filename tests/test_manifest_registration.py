"""Модульные проверки прямой регистрации документов в реестрах."""

from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from typing import Any

from src.collect.base import Document, HttpResponseSnapshot
from src.corpus.identity import canonicalize_url
from src.corpus.manifests import ManifestConflictError, ManifestPlan, ManifestStore
from src.corpus.profiles import get_source_profile
from src.corpus.registration import (
    RegistrationOptions,
    plan_document,
    reconcile_document_plan,
    resolve_collection_rights,
)
from src.corpus.schema_validation import SchemaCatalog

ROOT = Path(__file__).resolve().parents[1]
FIRST_TIMESTAMP = "2026-08-30T10:00:00+03:00"
SECOND_TIMESTAMP = "2026-08-30T11:00:00+03:00"


class ManifestRegistrationTests(unittest.TestCase):
    """Проверки построения и согласования одного реестрового плана."""

    def setUp(self) -> None:
        """Создать профили, параметры и временное хранилище реестров."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )
        self.profile = get_source_profile("ufn")
        self.schema_catalog = SchemaCatalog(ROOT / "manifests" / "schemas")
        self.options = RegistrationOptions(
            content_role="title_abstract",
            acquisition_method="crawler",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="html_to_text",
            extraction_version="html-v1",
            response_representation="html",
            request_context_type="work",
        )

    def tearDown(self) -> None:
        """Удалить временное хранилище после проверки."""

        self.temporary_directory.cleanup()

    def document(self, **changes: Any) -> Document:
        """Создать синтетическую карточку статьи УФН."""

        values: dict[str, Any] = {
            "source": "ufn.ru",
            "url": "https://ufn.ru/ru/articles/2024/1/a/",
            "title": "Квантовые свойства плазмы",
            "text": "Аннотация физической статьи на русском языке.",
            "authors": ["Иванов И. И."],
            "published": "Wed, 10 Jan 2024 10:00:00 +0300",
            "section": "Обзоры актуальных проблем",
            "extra": {"year": "2024", "text_source": "html"},
        }
        values.update(changes)

        return Document(**values)

    def response(
        self,
        *,
        retrieved_at: str = FIRST_TIMESTAMP,
        body: bytes = b"<html><main>raw article</main></html>",
    ) -> HttpResponseSnapshot:
        """Создать синтетический успешный HTTP-снимок."""

        url = "https://ufn.ru/ru/articles/2024/1/a/"

        return HttpResponseSnapshot(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers=(("content-type", "text/html; charset=windows-1251"),),
            retrieved_at=retrieved_at,
            body=body,
        )

    def right(self, operation: str) -> dict[str, Any]:
        """Создать разрешающую запись права для записи тестового плана."""

        return {
            "schema_version": "rights-v1",
            "created_at": FIRST_TIMESTAMP,
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": "allowed",
            "access_basis": "Синтетическое разрешение для модульного теста.",
            "basis_type": "explicit_license",
            "acquisition_method": "crawler" if operation == "acquisition" else None,
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2026-08-30",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def add_rights(self, plan: ManifestPlan) -> None:
        """Добавить в первый план две необходимые записи прав."""

        plan.rights = [self.right("acquisition"), self.right("storage")]

    def fulfilment(
        self,
        *,
        rights_record_id: str,
        subject_id: str,
    ) -> dict[str, Any]:
        """Создать доказательство выполнения одного тестового условия."""

        return {
            "schema_version": "condition-fulfilments-v1",
            "fulfilment_id": f"fulfilment-{rights_record_id}-{subject_id}",
            "created_at": FIRST_TIMESTAMP,
            "rights_record_id": rights_record_id,
            "condition": "Закрытое исследование.",
            "subject_type": "project",
            "subject_id": subject_id,
            "status": "satisfied",
            "satisfied_at": FIRST_TIMESTAMP,
            "expires_at": None,
            "evidence_sha256": "b" * 64,
            "supersedes_fulfilment_id": None,
        }

    def validate_plan_records(self, plan: ManifestPlan) -> None:
        """Проверить все сформированные записи по действующим JSON Schema."""

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in getattr(plan, kind):
                self.schema_catalog.validate(kind, record)

    def test_work_response_creates_raw_parent_and_derived_text(self) -> None:
        """Ответ статьи должен стать родителем производного текста."""

        plan = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )

        self.validate_plan_records(plan)
        self.assertEqual(len(plan.works), 1)
        self.assertEqual(len(plan.artifacts), 2)
        self.assertEqual(len(plan.blobs), 2)
        raw_artifact, text_artifact = plan.artifacts
        self.assertEqual(text_artifact["parent_artifact_id"], raw_artifact["artifact_id"])
        self.assertEqual(raw_artifact["representation"], "html")
        self.assertEqual(text_artifact["representation"], "plain_text")
        self.assertEqual(text_artifact["content_role"], "title_abstract")
        self.assertEqual(plan.works[0]["published_at"], "2024-01-10")
        self.assertEqual(plan.work_aliases[0]["alias_type"], "source_native_id")

    def test_collection_rights_are_resolved_before_network_access(self) -> None:
        """Предварительная проверка должна выбрать acquisition и storage."""

        self.store.commit(
            ManifestPlan(
                rights=[self.right("acquisition"), self.right("storage")]
            )
        )

        selected = resolve_collection_rights(
            self.store,
            self.profile,
            acquisition_method="crawler",
            acquisition_scope="sample",
            allowed_rights_record_ids=(
                "right-storage",
                "right-acquisition",
            ),
        )

        self.assertEqual(
            selected,
            ("right-acquisition", "right-storage"),
        )

    def test_other_project_cannot_satisfy_collection_condition(self) -> None:
        """Доказательство другого проекта не должно разрешать сетевой сбор."""

        acquisition = self.right("acquisition")
        acquisition["status"] = "conditional"
        acquisition["rights_conditions"] = ["Закрытое исследование."]
        self.store.commit(
            ManifestPlan(
                rights=[acquisition, self.right("storage")],
                condition_fulfilments=[
                    self.fulfilment(
                        rights_record_id="right-acquisition",
                        subject_id="another-project",
                    )
                ],
            )
        )

        with self.assertRaisesRegex(
            ManifestConflictError,
            "Операция acquisition не разрешена",
        ):
            resolve_collection_rights(
                self.store,
                self.profile,
                acquisition_method="crawler",
                acquisition_scope="sample",
                allowed_rights_record_ids=(
                    "right-acquisition",
                    "right-storage",
                ),
            )

    def test_source_response_has_event_and_blob_without_raw_artifact(self) -> None:
        """Общий RSS-ответ не должен ошибочно принадлежать одной работе."""

        options = RegistrationOptions(
            content_role="title_abstract",
            acquisition_method="crawler",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="rss_summary_to_text",
            extraction_version="rss-v1",
            response_representation="rss",
            request_context_type="source",
        )
        response = self.response(body=b"<rss><channel /></rss>")
        plan = plan_document(
            self.document(source="ufn.ru:rss"),
            self.profile,
            options,
            FIRST_TIMESTAMP,
            response,
        )

        self.validate_plan_records(plan)
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(plan.artifacts[0]["representation"], "plain_text")
        self.assertEqual(len(plan.blobs), 2)
        event = plan.retrieval_events[0]
        self.assertEqual(event["request_context_type"], "source")
        self.assertEqual(event["request_context_id"], self.profile.source_id)
        self.assertTrue(event["response_path"].startswith("data/raw/http/"))

    def test_missing_response_creates_honest_metadata_only_event(self) -> None:
        """Старый Document не должен получать выдуманные HTTP-метаданные."""

        plan = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
        )

        self.validate_plan_records(plan)
        event = plan.retrieval_events[0]
        self.assertEqual(event["outcome"], "metadata_only")
        self.assertIsNone(event["final_url"])
        self.assertIsNone(event["http_status"])
        self.assertEqual(event["response_headers"], {})
        self.assertIsNone(event["response_path"])
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(plan.blobs), 1)

    def test_full_text_requires_saved_source_response(self) -> None:
        """Полный текст нельзя объявлять без сохранённого исходного ответа."""

        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="crawler",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="html_to_text",
            extraction_version="html-v1",
            response_representation="html",
            request_context_type="work",
        )

        with self.assertRaisesRegex(ValueError, "HttpResponseSnapshot"):
            plan_document(
                self.document(),
                self.profile,
                options,
                FIRST_TIMESTAMP,
            )

        foreign_response = HttpResponseSnapshot(
            requested_url="https://example.invalid/foreign.pdf",
            final_url="https://example.invalid/foreign.pdf",
            status_code=200,
            headers=(("content-type", "application/pdf"),),
            retrieved_at=FIRST_TIMESTAMP,
            body=b"foreign",
        )

        with self.assertRaisesRegex(ValueError, "requested_url"):
            plan_document(
                self.document(),
                self.profile,
                options,
                FIRST_TIMESTAMP,
                foreign_response,
            )

        with self.assertRaisesRegex(ValueError, "непустое"):
            plan_document(
                self.document(),
                self.profile,
                options,
                FIRST_TIMESTAMP,
                self.response(body=b""),
            )

    def test_same_inputs_produce_stable_identifiers_and_paths(self) -> None:
        """Повтор одного снимка должен дать те же ID, хеши и пути."""

        first = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        second = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )

        self.assertEqual(first.works, second.works)
        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(first.retrieval_events, second.retrieval_events)
        self.assertEqual(first.work_aliases, second.work_aliases)
        self.assertEqual(first.blobs, second.blobs)

    def test_same_http_snapshot_remains_immutable_at_later_registration(self) -> None:
        """Повтор одного HTTP-снимка не должен менять событие."""

        response = self.response()
        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            response,
        )
        self.add_rights(initial)
        self.store.commit(initial)
        candidate = plan_document(
            self.document(),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            response,
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        self.assertEqual(
            reconciled.retrieval_events[0],
            initial.retrieval_events[0],
        )
        result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )
        self.assertEqual(result.unchanged["retrieval_events"], 1)

    def test_late_doi_preserves_work_id_and_adds_verified_alias(self) -> None:
        """Поздний DOI должен дополнить, а не переименовать существующую работу."""

        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        preserved_work_id = initial.works[0]["work_id"]
        extra = {
            "year": "2024",
            "text_source": "html",
            "doi": "https://doi.org/10.1000/PHYS.1",
        }
        candidate = plan_document(
            self.document(extra=extra),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            self.response(retrieved_at=SECOND_TIMESTAMP),
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        work = reconciled.works[0]
        self.assertEqual(work["work_id"], preserved_work_id)
        self.assertEqual(work["doi"], "10.1000/phys.1")
        self.assertIn("doi:10.1000/phys.1", work["work_aliases"])
        doi_aliases = [
            record
            for record in reconciled.work_aliases
            if record["alias_type"] == "doi"
        ]
        self.assertEqual(len(doi_aliases), 1)
        self.assertEqual(doi_aliases[0]["alias_value"], "10.1000/phys.1")
        result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )
        self.assertEqual(result.updated["works"], 1)

    def test_late_doi_survives_unrelated_title_conflict(self) -> None:
        """Поздний DOI должен сохраниться при конфликте названия."""

        response = self.response()
        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            response,
        )
        self.add_rights(initial)
        self.store.commit(initial)
        preserved_work_id = initial.works[0]["work_id"]
        initial_retrieval_id = initial.retrieval_events[0]["retrieval_id"]
        candidate = plan_document(
            self.document(
                title="Несовместимое название",
                extra={
                    "year": "2024",
                    "text_source": "html",
                    "doi": "10.1000/phys.1",
                },
            ),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            response,
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        work = reconciled.works[0]
        self.assertEqual(work["work_id"], preserved_work_id)
        self.assertEqual(work["title"], "Квантовые свойства плазмы")
        self.assertEqual(work["doi"], "10.1000/phys.1")
        self.assertEqual(work["eligibility_status"], "quarantined")
        self.assertIn("doi:10.1000/phys.1", work["work_aliases"])
        doi_alias = next(
            record
            for record in reconciled.work_aliases
            if record["alias_type"] == "doi"
        )
        self.assertEqual(doi_alias["work_id"], preserved_work_id)
        self.assertEqual(doi_alias["source_retrieval_id"], initial_retrieval_id)
        self.assertEqual(
            reconciled.retrieval_events[0]["retrieval_id"],
            initial_retrieval_id,
        )
        self.assertEqual(
            reconciled.retrieval_events[0]["request_context_id"],
            preserved_work_id,
        )
        self.assertEqual(
            reconciled.identity_conflicts[0]["source_retrieval_ids"],
            [initial_retrieval_id],
        )
        result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )
        self.assertEqual(result.unchanged["retrieval_events"], 1)
        self.assertEqual(len(self.store.records("work_aliases")), 3)

    def test_same_snapshot_with_late_doi_does_not_duplicate_events(self) -> None:
        """Поздний DOI не должен дублировать событие одного HTTP-снимка."""

        response = self.response()
        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            response,
        )
        self.add_rights(initial)
        self.store.commit(initial)
        retrieval_id = initial.retrieval_events[0]["retrieval_id"]
        initial_decision_ids = {
            record["decision_id"]
            for record in self.store.records("operation_decisions")
        }
        late_document = self.document(
            extra={
                "year": "2024",
                "text_source": "html",
                "doi": "10.1000/phys.1",
            }
        )
        candidate = plan_document(
            late_document,
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            response,
        )
        self.assertNotEqual(candidate.retrieval_events[0]["retrieval_id"], retrieval_id)
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)
        self.assertEqual(reconciled.retrieval_events[0]["retrieval_id"], retrieval_id)
        first_result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )

        repeated = plan_document(
            late_document,
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            response,
        )
        reconciled_repeat, repeated_hashes = reconcile_document_plan(
            self.store,
            repeated,
        )
        second_result = self.store.commit(
            reconciled_repeat,
            expected_snapshot_hashes=repeated_hashes,
        )

        self.assertEqual(first_result.unchanged["retrieval_events"], 1)
        self.assertEqual(second_result.unchanged["retrieval_events"], 1)
        self.assertEqual(second_result.inserted["operation_decisions"], 0)
        self.assertEqual(len(self.store.records("retrieval_events")), 1)
        self.assertEqual(
            {
                record["decision_id"]
                for record in self.store.records("operation_decisions")
            },
            initial_decision_ids,
        )

    def test_late_edn_adds_verified_alias_without_changing_work_id(self) -> None:
        """Поздний EDN должен пополнить карточку и историю псевдонимов."""

        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        preserved_work_id = initial.works[0]["work_id"]
        candidate = plan_document(
            self.document(
                extra={
                    "year": "2024",
                    "text_source": "html",
                    "edn": "ABCDEF",
                }
            ),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            self.response(retrieved_at=SECOND_TIMESTAMP),
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        work = reconciled.works[0]
        self.assertEqual(work["work_id"], preserved_work_id)
        self.assertEqual(work["edn"], "ABCDEF")
        self.assertIn("edn:ABCDEF", work["work_aliases"])
        edn_aliases = [
            record
            for record in reconciled.work_aliases
            if record["alias_type"] == "edn"
        ]
        self.assertEqual(len(edn_aliases), 1)
        self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )

    def test_alternate_url_becomes_verified_alias(self) -> None:
        """Новый URL той же DOI-работы должен стать псевдонимом."""

        doi_extra = {
            "year": "2024",
            "text_source": "html",
            "doi": "10.1000/phys.1",
        }
        initial = plan_document(
            self.document(extra=doi_extra),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        alternate_url = "https://ufn.ru/ru/articles/2024/1/b/"
        alternate_response = HttpResponseSnapshot(
            requested_url=alternate_url,
            final_url=alternate_url,
            status_code=200,
            headers=(("content-type", "text/html"),),
            retrieved_at=SECOND_TIMESTAMP,
            body=b"<html><main>raw article</main></html>",
        )
        candidate = plan_document(
            self.document(url=alternate_url, extra=doi_extra),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            alternate_response,
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        work = reconciled.works[0]
        self.assertEqual(work["canonical_url"], initial.works[0]["canonical_url"])
        canonical_alternate_url = canonicalize_url(alternate_url)
        self.assertIn(f"url:{canonical_alternate_url}", work["work_aliases"])
        url_aliases = [
            record
            for record in reconciled.work_aliases
            if record["alias_type"] == "canonical_url"
        ]
        self.assertEqual(len(url_aliases), 1)
        self.assertEqual(url_aliases[0]["alias_value"], canonical_alternate_url)
        self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )

    def test_known_work_prohibition_blocks_collection_preflight(self) -> None:
        """Узкий запрет известной работы должен сработать до сети."""

        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        prohibition = self.right("acquisition")
        prohibition.update(
            {
                "created_at": SECOND_TIMESTAMP,
                "rights_record_id": "right-work-prohibited",
                "scope_type": "work",
                "scope_id": initial.works[0]["work_id"],
                "status": "prohibited",
                "access_basis": "Синтетический точечный запрет.",
            }
        )
        self.store.commit(ManifestPlan(rights=[prohibition]))

        with self.assertRaisesRegex(ManifestConflictError, "До сетевого обхода"):
            resolve_collection_rights(
                self.store,
                self.profile,
                acquisition_method="crawler",
                acquisition_scope="sample",
                allowed_rights_record_ids=(
                    "right-acquisition",
                    "right-storage",
                ),
            )

    def test_conflicting_title_quarantines_existing_work(self) -> None:
        """Два непустых названия не должны объединяться эвристически."""

        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        candidate = plan_document(
            self.document(title="Несовместимое название"),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            self.response(retrieved_at=SECOND_TIMESTAMP),
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        work = reconciled.works[0]
        self.assertEqual(work["title"], "Квантовые свойства плазмы")
        self.assertEqual(work["eligibility_status"], "quarantined")
        self.assertEqual(len(reconciled.identity_conflicts), 1)
        self.assertEqual(reconciled.identity_conflicts[0]["field"], "title")
        result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )
        self.assertEqual(result.updated["works"], 1)

    def test_same_bytes_with_new_retrieval_updates_artifact_projection(self) -> None:
        """Повтор тех же байтов должен добавить событие, а не новый артефакт."""

        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        candidate = plan_document(
            self.document(),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            self.response(retrieved_at=SECOND_TIMESTAMP),
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)

        self.assertEqual(len(reconciled.artifacts), 2)
        self.assertTrue(reconciled.artifact_update_reasons)

        for artifact in reconciled.artifacts:
            self.assertEqual(len(artifact["retrievals"]), 2)

        result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )
        self.assertEqual(result.inserted["artifacts"], 0)
        self.assertEqual(result.updated["artifacts"], 2)
        self.assertEqual(len(self.store.records("artifacts")), 2)

    def test_same_payload_is_not_silently_reassigned_to_another_work(self) -> None:
        """Одинаковые байты разных работ должны требовать ручного решения."""

        initial = plan_document(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )
        self.add_rights(initial)
        self.store.commit(initial)
        other_url = "https://ufn.ru/ru/articles/2024/1/b/"
        other_response = HttpResponseSnapshot(
            requested_url=other_url,
            final_url=other_url,
            status_code=200,
            headers=(("content-type", "text/html"),),
            retrieved_at=SECOND_TIMESTAMP,
            body=self.response().body,
        )
        candidate = plan_document(
            self.document(
                url=other_url,
                title="Другая статья с ошибочно совпавшим содержимым",
            ),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            other_response,
        )

        with self.assertRaises(ManifestConflictError):
            reconcile_document_plan(self.store, candidate)


if __name__ == "__main__":
    unittest.main()
