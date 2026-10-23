"""Проверки регистрации точных HTTP-ответов без производного текста."""

from __future__ import annotations

import tempfile
import unittest

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from src.collect.base import Document, HttpResponseSnapshot
from src.corpus.identity import canonicalize_url
from src.corpus.manifests import (
    ManifestConflictError,
    ManifestPlan,
    ManifestStore,
    sha256_bytes,
)
from src.corpus.profiles import get_source_profile
from src.corpus.registration import (
    RegistrationOptions,
    _retrieval_event_id,
    plan_document,
    plan_http_response,
    reconcile_document_plan,
)
from src.corpus.schema_validation import SchemaCatalog

ROOT = Path(__file__).resolve().parents[1]
FIRST_TIMESTAMP = "2026-10-13T12:00:00+03:00"
SECOND_TIMESTAMP = "2026-10-14T12:00:00+03:00"
ARTICLE_URL = "https://ufn.ru/ru/articles/2024/1/a/"
PDF_URL = "https://ufn.ru/ufn2024/ufn2024_1/Russian/r241a.pdf"
PDF_BYTES = b"%PDF-1.7\nsynthetic response\n%%EOF\n"


class ResponseRegistrationTests(unittest.TestCase):
    """Проверки исходных артефактов, их происхождения и повторного импорта."""

    def setUp(self) -> None:
        """Создать отдельное временное хранилище и синтетический профиль."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )
        self.catalog = SchemaCatalog(ROOT / "manifests" / "schemas")
        self.profile = get_source_profile("ufn")
        self.options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="crawler",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )

    def tearDown(self) -> None:
        """Удалить временные реестры и байты после проверки."""

        self.temporary_directory.cleanup()

    def document(self, **changes: Any) -> Document:
        """Создать карточку с аннотацией, не выдавая её за полный текст."""

        values: dict[str, Any] = {
            "source": "ufn.ru",
            "url": ARTICLE_URL,
            "title": "Квантовые свойства плазмы",
            "text": "Только аннотация, не полный текст.",
            "authors": ["Иванов И. И."],
            "published": "2024-01-10",
            "pdf_url": PDF_URL,
            "extra": {"abstract": "Только аннотация, не полный текст."},
        }
        values.update(changes)

        return Document(**values)

    def response(self, **changes: Any) -> HttpResponseSnapshot:
        """Создать точный синтетический HTTP-ответ с фиксированным временем."""

        values: dict[str, Any] = {
            "requested_url": PDF_URL,
            "final_url": PDF_URL,
            "status_code": 200,
            "headers": (("content-type", "application/pdf"),),
            "retrieved_at": FIRST_TIMESTAMP,
            "body": PDF_BYTES,
        }
        values.update(changes)

        return HttpResponseSnapshot(**values)

    def metadata_options(self, *, context: str = "work") -> RegistrationOptions:
        """Выбрать регистрацию исходной HTML-карточки или общего оглавления."""

        return replace(
            self.options,
            content_role="metadata_only",
            response_representation="html",
            request_context_type=context,
        )

    def right(self, operation: str) -> dict[str, Any]:
        """Создать синтетическое разрешение только на получение или хранение."""

        return {
            "schema_version": "rights-v1",
            "created_at": "2026-10-12T12:00:00+03:00",
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": "allowed",
            "access_basis": "Синтетическое разрешение для модульной проверки.",
            "basis_type": "explicit_license",
            "acquisition_method": "crawler" if operation == "acquisition" else None,
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2026-10-12",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def validate_plan(self, plan: ManifestPlan) -> None:
        """Проверить записи по действующим JSON Schema."""

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in getattr(plan, kind):
                self.catalog.validate(kind, record)

    def test_pdf_creates_only_original_without_annotation_text(self) -> None:
        """PDF должен остаться единственным артефактом без фиктивного TXT."""

        response = self.response()
        plan = plan_http_response(
            self.document(), self.profile, self.options, FIRST_TIMESTAMP, response,
        )

        self.validate_plan(plan)
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(plan.blobs), 1)
        artifact = plan.artifacts[0]
        self.assertEqual(artifact["representation"], "pdf")
        self.assertEqual(artifact["content_role"], "full_text")
        self.assertEqual(artifact["sha256"], sha256_bytes(PDF_BYTES))
        self.assertEqual(artifact["extraction_status"], "not_started")
        self.assertEqual(artifact["processing_status"], "not_started")
        self.assertIsNone(artifact["parent_artifact_id"])

        for field in ("extraction_method", "extraction_version", "characters", "words"):
            self.assertIsNone(artifact[field])

        self.assertEqual(plan.blobs[0].data, PDF_BYTES)
        self.assertTrue(plan.blobs[0].relative_path.endswith(".pdf"))
        event = plan.retrieval_events[0]
        self.assertEqual(event["response_metadata_sha256"], response.metadata_sha256())
        self.assertEqual(event["response_sha256"], artifact["sha256"])
        self.assertEqual(event["response_path"], artifact["path"])
        self.assertEqual(plan.works[0]["abstract"], self.document().extra["abstract"])
        self.assertEqual(plan.works[0]["eligibility_status"], "pending")
        self.assertEqual(plan.rights, [])

    def test_empty_document_text_is_allowed_for_pdf(self) -> None:
        """Исходный PDF не требует предварительно извлекать какой-либо текст."""

        plan = plan_http_response(
            self.document(text=""),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(),
        )

        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(plan.blobs[0].data, PDF_BYTES)

    def test_late_registration_preserves_original_download_time(self) -> None:
        """Дата внесения события и псевдонима не должна подменять скачивание."""

        response = self.response()
        plan = plan_http_response(
            self.document(), self.profile, self.options, SECOND_TIMESTAMP, response,
        )
        self.validate_plan(plan)
        event = plan.retrieval_events[0]
        alias = plan.work_aliases[0]
        downloaded_at = datetime.fromisoformat(FIRST_TIMESTAMP)
        registered_at = datetime.fromisoformat(SECOND_TIMESTAMP)

        self.assertEqual(datetime.fromisoformat(event["retrieved_at"]), downloaded_at)
        self.assertEqual(datetime.fromisoformat(event["created_at"]), registered_at)
        self.assertEqual(datetime.fromisoformat(alias["verified_at"]), downloaded_at)
        self.assertEqual(datetime.fromisoformat(alias["created_at"]), registered_at)
        self.assertEqual(event["response_metadata_sha256"], response.metadata_sha256())
        self.assertEqual(
            datetime.fromisoformat(plan.artifacts[0]["created_at"]), registered_at,
        )

    def test_registration_cannot_precede_download(self) -> None:
        """Позднюю регистрацию нельзя искусственно датировать раньше ответа."""

        for timestamp in (
            "2026-10-13T11:59:59+03:00",
            "2026-10-13T09:59:59+01:00",
            "2026-10-13T13:00:00+05:00",
        ):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(
                ValueError, "раньше фактического времени",
            ):
                plan_http_response(
                    self.document(),
                    self.profile,
                    self.options,
                    timestamp,
                    self.response(),
                )

    def test_late_rights_are_visible_at_actual_registration_time(self) -> None:
        """Ранее действовавшее основание учитывается при честной поздней записи."""

        registration_timestamp = "2026-10-13T13:00:00+03:00"
        plan = plan_http_response(
            self.document(),
            self.profile,
            self.options,
            registration_timestamp,
            self.response(),
        )
        plan.rights = [self.right("acquisition"), self.right("storage")]

        for right in plan.rights:
            right.update(
                {
                    "created_at": registration_timestamp,
                    "rights_checked_at": "2026-10-13",
                    "effective_from": FIRST_TIMESTAMP,
                    "effective_from_evidence_sha256": "b" * 64,
                }
            )

        self.store.commit(plan)
        self.assertTrue(self.store.audit().ok)
        event = plan.retrieval_events[0]
        acquisition = next(
            decision
            for decision in self.store.records("operation_decisions")
            if decision["operation"] == "acquisition"
            and decision["subject_type"] == "retrieval"
            and decision["subject_id"] == event["retrieval_id"]
        )
        self.assertEqual(acquisition["decision_at"], event["retrieved_at"])
        self.assertEqual(acquisition["knowledge_cutoff_at"], event["created_at"])
        self.assertEqual(acquisition["status"], "allowed")
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "pending")

    def test_equal_download_in_another_timezone_is_supported(self) -> None:
        """Часовой пояс не должен менять сравнение фактических моментов."""

        plan = plan_http_response(
            self.document(),
            self.profile,
            self.options,
            "2026-10-13T09:00:00+00:00",
            self.response(),
        )
        event = plan.retrieval_events[0]

        self.assertEqual(
            datetime.fromisoformat(event["created_at"]),
            datetime.fromisoformat(event["retrieved_at"]),
        )

    def test_legacy_registration_keeps_original_event_timestamp(self) -> None:
        """Прежний API не меняет дату события при поздней обработке текста."""

        plan = plan_document(
            self.document(),
            self.profile,
            self.options,
            SECOND_TIMESTAMP,
            self.response(),
        )
        event = plan.retrieval_events[0]

        self.assertEqual(event["created_at"], event["retrieved_at"])
        self.assertEqual(
            datetime.fromisoformat(event["created_at"]),
            datetime.fromisoformat(FIRST_TIMESTAMP),
        )

    def test_empty_metadata_text_creates_only_html(self) -> None:
        """HTML-карточка без аннотации не должна порождать пустой TXT."""

        response = self.response(
            requested_url=ARTICLE_URL,
            final_url=ARTICLE_URL,
            body=b"<html>metadata</html>",
            headers=(("content-type", "text/html; charset=utf-8"),),
        )
        plan = plan_http_response(
            self.document(text="", extra={}),
            self.profile,
            self.metadata_options(),
            FIRST_TIMESTAMP,
            response,
        )

        self.validate_plan(plan)
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(plan.artifacts[0]["representation"], "html")
        self.assertEqual(plan.artifacts[0]["content_role"], "metadata_only")
        self.assertEqual(plan.blobs[0].data, response.body)
        self.assertIsNone(plan.works[0]["abstract"])

    def test_source_toc_is_not_a_work_scoped_artifact(self) -> None:
        """Общее оглавление двух статей хранится один раз и без общего артефакта."""

        response = self.response(
            requested_url="https://ufn.ru/ru/issues/2024/1/",
            final_url="https://ufn.ru/ru/issues/2024/1/",
            body=b"<html>two articles</html>",
            headers=(("content-type", "text/html"),),
        )
        plans = [
            plan_http_response(
                document,
                self.profile,
                self.metadata_options(context="source"),
                FIRST_TIMESTAMP,
                response,
            )
            for document in (
                self.document(text=""),
                self.document(
                    url="https://ufn.ru/ru/articles/2024/1/b/",
                    title="Вторая статья об оптике",
                    text="",
                ),
            )
        ]
        plans[0].rights = [self.right("acquisition"), self.right("storage")]

        for plan in plans:
            self.validate_plan(plan)
            self.assertEqual(plan.artifacts, [])
            self.assertEqual(len(plan.blobs), 1)
            self.assertEqual(len(plan.works), 1)
            self.assertEqual(len(plan.work_aliases), 1)
            event = plan.retrieval_events[0]
            self.assertEqual(event["request_context_type"], "source")
            self.assertEqual(event["request_context_id"], self.profile.source_id)
            self.assertEqual(event["requested_url"], response.requested_url)
            self.assertEqual(event["final_url"], response.final_url)
            self.store.commit(plan)

        self.assertEqual(len(self.store.records("works")), 2)
        self.assertEqual(len(self.store.records("retrieval_events")), 1)
        self.assertEqual(self.store.records("artifacts"), [])
        self.assertTrue(self.store.audit().ok)

    def test_response_keeps_exact_slash_and_query_but_work_url_is_canonical(self) -> None:
        """Событие и его ID сохраняют точный HTTP-адрес, в отличие от карточки."""

        requested_url = ARTICLE_URL + "?z=2&a=one%20word&utm_source=pilot"
        final_url = ARTICLE_URL + "?a=one%20word&z=2&utm_source=final"
        response = self.response(
            requested_url=requested_url,
            final_url=final_url,
            body=b"<html>metadata</html>",
            headers=(("content-type", "text/html"),),
        )
        plan = plan_http_response(
            self.document(url=requested_url, text=""),
            self.profile,
            self.metadata_options(),
            FIRST_TIMESTAMP,
            response,
        )

        self.validate_plan(plan)
        event = plan.retrieval_events[0]
        self.assertEqual(event["requested_url"], requested_url)
        self.assertEqual(event["final_url"], final_url)
        self.assertEqual(event["response_metadata_sha256"], response.metadata_sha256())
        self.assertEqual(plan.works[0]["canonical_url"], canonicalize_url(requested_url))
        self.assertEqual(plan.artifacts[0]["retrievals"][0]["retrieved_url"], final_url)
        self.assertEqual(event["retrieval_id"], _retrieval_event_id(event))
        canonical_event = {
            **event,
            "requested_url": canonicalize_url(requested_url),
            "final_url": canonicalize_url(final_url),
        }
        self.assertNotEqual(event["retrieval_id"], _retrieval_event_id(canonical_event))

        plan.rights = [self.right("acquisition"), self.right("storage")]
        self.store.commit(plan)
        self.assertTrue(self.store.audit().ok)
        stored_event = self.store.records("retrieval_events")[0]
        self.assertEqual(stored_event["requested_url"], requested_url)
        self.assertEqual(stored_event["final_url"], final_url)

    def test_existing_document_registration_keeps_canonical_response_urls(self) -> None:
        """Прежний API сохраняет прежнее правило канонизации URL события."""

        requested_url = ARTICLE_URL + "?z=2&a=one%20word&utm_source=pilot"
        response = self.response(
            requested_url=requested_url,
            final_url=requested_url,
            body=b"<html>metadata</html>",
            headers=(("content-type", "text/html"),),
        )
        plan = plan_document(
            self.document(url=requested_url),
            self.profile,
            self.metadata_options(),
            FIRST_TIMESTAMP,
            response,
        )

        self.assertEqual(
            plan.retrieval_events[0]["requested_url"], canonicalize_url(requested_url),
        )
        self.assertEqual(
            plan.retrieval_events[0]["final_url"], canonicalize_url(requested_url),
        )

    def test_source_context_cannot_claim_full_text(self) -> None:
        """Общий ответ источника нельзя объявить полным текстом одной работы."""

        with self.assertRaisesRegex(ValueError, "full_text"):
            plan_http_response(
                self.document(),
                self.profile,
                replace(self.options, request_context_type="source"),
                FIRST_TIMESTAMP,
                self.response(),
            )

    def test_artifact_context_uses_original_response_id(self) -> None:
        """Контекст artifact должен ссылаться на исходный PDF, не на пустой текст."""

        plan = plan_http_response(
            self.document(text=""),
            self.profile,
            replace(self.options, request_context_type="artifact"),
            FIRST_TIMESTAMP,
            self.response(),
        )

        self.validate_plan(plan)
        self.assertEqual(
            plan.retrieval_events[0]["request_context_id"],
            plan.artifacts[0]["artifact_record_id"],
        )

    def test_repeat_import_does_not_change_snapshots_or_duplicate_history(self) -> None:
        """Повторная регистрация точного ответа не создаёт ревизий или дублей."""

        initial = plan_http_response(
            self.document(), self.profile, self.options, FIRST_TIMESTAMP, self.response(),
        )
        initial.rights = [self.right("acquisition"), self.right("storage")]
        self.store.commit(initial)
        hashes_before = self.store.snapshot_hashes()
        repeated = plan_http_response(
            self.document(), self.profile, self.options, SECOND_TIMESTAMP, self.response(),
        )
        self.assertNotEqual(
            repeated.retrieval_events[0]["created_at"],
            initial.retrieval_events[0]["created_at"],
        )
        self.assertNotEqual(
            repeated.work_aliases[0]["created_at"],
            initial.work_aliases[0]["created_at"],
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, repeated)
        self.assertEqual(reconciled.retrieval_events, initial.retrieval_events)
        self.assertEqual(reconciled.work_aliases, initial.work_aliases)
        result = self.store.commit(
            reconciled,
            expected_snapshot_hashes=expected_hashes,
        )

        self.assertEqual(self.store.snapshot_hashes(), hashes_before)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(len(self.store.records("artifacts")), 1)
        self.assertEqual(len(self.store.records("retrieval_events")), 1)
        self.assertEqual(len(self.store.records("work_aliases")), 1)
        self.assertEqual(result.written_blobs, 0)
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "pending")
        self.assertTrue(self.store.audit().ok)

    def test_same_retrieval_id_cannot_hide_other_provenance_changes(self) -> None:
        """Сохранение первой даты не должно скрывать другое изменение события."""

        initial = plan_http_response(
            self.document(), self.profile, self.options, FIRST_TIMESTAMP, self.response(),
        )
        initial.rights = [self.right("acquisition"), self.right("storage")]
        self.store.commit(initial)
        hashes_before = self.store.snapshot_hashes()
        repeated = plan_http_response(
            self.document(), self.profile, self.options, SECOND_TIMESTAMP, self.response(),
        )
        repeated.retrieval_events[0]["response_path"] = "data/raw/other-response.pdf"

        with self.assertRaisesRegex(ManifestConflictError, "retrieval_id=.*не совпадает"):
            reconcile_document_plan(self.store, repeated)

        self.assertEqual(self.store.snapshot_hashes(), hashes_before)

    def test_same_alias_id_cannot_hide_other_history_changes(self) -> None:
        """Сохранение первой даты псевдонима не скрывает подмену его истории."""

        initial = plan_http_response(
            self.document(), self.profile, self.options, FIRST_TIMESTAMP, self.response(),
        )
        initial.rights = [self.right("acquisition"), self.right("storage")]
        self.store.commit(initial)
        hashes_before = self.store.snapshot_hashes()
        repeated = plan_http_response(
            self.document(), self.profile, self.options, SECOND_TIMESTAMP, self.response(),
        )
        repeated.work_aliases[0]["supersedes_alias_record_id"] = "work-alias:other"

        with self.assertRaisesRegex(ManifestConflictError, "alias_record_id=.*не совпадает"):
            reconcile_document_plan(self.store, repeated)

        self.assertEqual(self.store.snapshot_hashes(), hashes_before)

    def test_late_doi_keeps_native_identity_and_adds_verified_alias(self) -> None:
        """Поздний DOI не меняет work_id и не создаёт повторный HTTP-запрос."""

        initial = plan_http_response(
            self.document(), self.profile, self.options, FIRST_TIMESTAMP, self.response(),
        )
        initial.rights = [self.right("acquisition"), self.right("storage")]
        self.store.commit(initial)
        original_work_id = initial.works[0]["work_id"]
        document = self.document()
        document.extra["doi"] = "https://doi.org/10.1000/PHYS.1"
        candidate = plan_http_response(
            document, self.profile, self.options, SECOND_TIMESTAMP, self.response(),
        )
        reconciled, expected_hashes = reconcile_document_plan(self.store, candidate)
        self.store.commit(reconciled, expected_snapshot_hashes=expected_hashes)

        work = self.store.records("works")[0]
        self.assertEqual(work["work_id"], original_work_id)
        self.assertEqual(work["doi"], "10.1000/phys.1")
        self.assertIn("doi:10.1000/phys.1", work["work_aliases"])
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(len(self.store.records("artifacts")), 1)
        self.assertEqual(len(self.store.records("retrieval_events")), 1)
        alias_types = {record["alias_type"] for record in self.store.records("work_aliases")}
        self.assertEqual(alias_types, {"source_native_id", "doi"})
        self.assertTrue(self.store.audit().ok)

    def test_invalid_document_identity_is_rejected(self) -> None:
        """Заголовок, источник, URL и соответствие профилю обязательны."""

        for changes in (
            {"title": " "},
            {"source": ""},
            {"source": "other.invalid"},
            {"url": "https://other.invalid/article"},
            {"url": "relative/article"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                plan_http_response(
                    self.document(**changes),
                    self.profile,
                    self.options,
                    FIRST_TIMESTAMP,
                    self.response(),
                )

    def test_unrelated_response_url_is_rejected(self) -> None:
        """Ответ другой статьи нельзя присвоить текущей работе."""

        response = self.response(requested_url="https://ufn.ru/other.pdf")

        with self.assertRaisesRegex(ValueError, "не соответствует"):
            plan_http_response(
                self.document(), self.profile, self.options, FIRST_TIMESTAMP, response,
            )

    def test_redirect_and_error_statuses_are_rejected(self) -> None:
        """Перенаправления и страницы ошибок не должны стать исходной статьёй."""

        for status_code in (301, 304, 403, 404, 500):
            with self.subTest(status_code=status_code), self.assertRaisesRegex(
                ValueError, "HTTP 2xx",
            ):
                plan_http_response(
                    self.document(),
                    self.profile,
                    self.options,
                    FIRST_TIMESTAMP,
                    self.response(status_code=status_code),
                )

    def test_successful_created_response_is_supported(self) -> None:
        """Успешный HTTP 201 также сохраняет точные байты ответа."""

        plan = plan_http_response(
            self.document(),
            self.profile,
            self.options,
            FIRST_TIMESTAMP,
            self.response(status_code=201),
        )

        self.assertEqual(plan.retrieval_events[0]["http_status"], 201)

    def test_empty_response_body_is_rejected(self) -> None:
        """Пустой HTTP-ответ не должен становиться исходным артефактом."""

        with self.assertRaisesRegex(ValueError, "Тело исходного HTTP-ответа"):
            plan_http_response(
                self.document(),
                self.profile,
                self.metadata_options(),
                FIRST_TIMESTAMP,
                self.response(body=b""),
            )

    def test_existing_document_registration_still_requires_text(self) -> None:
        """Прежний API регистрации производного текста остаётся совместимым."""

        with self.assertRaisesRegex(ValueError, "document.text"):
            plan_document(
                self.document(text=""),
                self.profile,
                self.options,
                FIRST_TIMESTAMP,
                self.response(),
            )

    def test_inventory_requires_original_response_without_processing(self) -> None:
        """Инвентарный режим не разрешает изготовить производный текст."""

        options = replace(self.options, inventory_only=True, rights_record_ids=())

        with self.assertRaisesRegex(ValueError, "Инвентарный учёт"):
            plan_document(self.document(), self.profile, options, FIRST_TIMESTAMP, self.response())

        with self.assertRaisesRegex(ValueError, "rights_record_ids"):
            replace(options, inventory_only=False)

        with self.assertRaisesRegex(ValueError, "логическим"):
            replace(self.options, inventory_only=1)

    def test_inventory_keeps_actual_response_and_quarantines_new_material(self) -> None:
        """Учёт без подтверждённых прав сохраняет байты, даты и отсутствие допуска."""

        options = replace(self.options, inventory_only=True, rights_record_ids=())
        plan = plan_http_response(self.document(), self.profile, options, SECOND_TIMESTAMP, self.response())

        self.validate_plan(plan)
        self.assertEqual(plan.works[0]["eligibility_status"], "quarantined")
        self.assertEqual(plan.artifacts[0]["acquisition_status"], "recorded")
        self.assertEqual(plan.artifacts[0]["qa_status"], "quarantined")
        self.assertEqual(plan.artifacts[0]["extraction_status"], "not_started")
        self.assertEqual(plan.retrieval_events[0]["outcome"], "recorded")
        self.assertEqual(
            datetime.fromisoformat(plan.retrieval_events[0]["retrieved_at"]),
            datetime.fromisoformat(self.response().retrieved_at),
        )
        self.assertEqual(plan.blobs[0].data, PDF_BYTES)
        self.assertEqual(plan.rights, [])

    def test_inventory_origin_does_not_replace_existing_artifact_status(self) -> None:
        """Повторные байты получают новое происхождение, но не теряют старый допуск."""

        accepted = plan_http_response(self.document(), self.profile, self.options, FIRST_TIMESTAMP, self.response())
        self.store.commit(ManifestPlan(rights=[self.right("acquisition"), self.right("storage")]))
        self.store.commit(accepted)
        previous_artifact = self.store.records("artifacts")[0]
        previous_work = self.store.records("works")[0]
        options = replace(self.options, inventory_only=True, rights_record_ids=())
        inventory = plan_http_response(self.document(), self.profile, options, SECOND_TIMESTAMP, self.response())

        reconciled, _ = reconcile_document_plan(self.store, inventory)

        self.assertEqual(reconciled.artifacts[0]["acquisition_status"], previous_artifact["acquisition_status"])
        self.assertEqual(reconciled.artifacts[0]["rights_record_ids"], previous_artifact["rights_record_ids"])
        self.assertEqual(reconciled.artifacts[0]["qa_status"], previous_artifact["qa_status"])
        self.assertEqual(reconciled.works[0]["eligibility_status"], previous_work["eligibility_status"])
        self.assertEqual(len(reconciled.artifacts[0]["retrievals"]), 2)


if __name__ == "__main__":
    unittest.main()
