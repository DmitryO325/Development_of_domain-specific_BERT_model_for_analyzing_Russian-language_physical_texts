"""Проверки инвентарной сверки HTTP-снимков без новых прав и изменения допуска."""

from __future__ import annotations

import copy
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.collect.base import Document
from src.corpus.journal_import import CollectedArticle
from src.corpus.journal_inventory_registration import reconcile_inventory_registration
from src.corpus.journal_registration import plan_collected_article
from src.corpus.manifests import ManifestConcurrencyError, ManifestPlan
from src.corpus.profiles import get_source_profile
from src.corpus.registration import RegistrationOptions, plan_http_response
from tests.test_journal_registration import JournalFixture, RIGHTS_IDS

REGISTERED_AT = "2026-10-14T13:00:00+00:00"


class JournalInventoryRegistrationTests(unittest.TestCase):
    """Проверять отдельные права, повторные ответы, конфликты и защиту от гонок."""

    def setUp(self) -> None:
        """Подготовить полностью локальную публикацию с синтетическим PDF."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = JournalFixture(Path(temporary.name))
        self.store = self.fixture.store()
        self.article = self.fixture.article()

    def _reconcile(self, articles: list[CollectedArticle] | None = None) -> tuple:
        """Подготовить план только во временном тестовом хранилище."""

        return reconcile_inventory_registration(
            self.store, articles if articles is not None else [self.article], registered_at=REGISTERED_AT,
        )

    def _register_success(self, *, eligibility: str = "pending") -> None:
        """Создать прежнюю успешную регистрацию с исключительно синтетическими правами."""

        self.store.commit(ManifestPlan(rights=self.fixture.rights()))
        plan = plan_collected_article(self.article, rights_record_ids=RIGHTS_IDS, registered_at=REGISTERED_AT)
        plan.works[0]["eligibility_status"] = eligibility

        if eligibility == "eligible":
            plan.works[0].update({"genre": "research_article", "published_at": "2026-10-01"})

        self.store.commit(plan)

    def test_inventory_without_rights_is_recorded_and_quarantined(self) -> None:
        """Наличие файлов без оснований не создаёт разрешений и допуска к обучению."""

        before = self.fixture.snapshot()
        plan, hashes, report = self._reconcile()

        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(report, [])
        self.assertEqual(plan.rights, [])
        self.assertEqual(plan.works[0]["eligibility_status"], "quarantined")
        self.assertTrue(all(artifact["acquisition_status"] == "recorded" for artifact in plan.artifacts))
        self.assertTrue(all(event["outcome"] == "recorded" for event in plan.retrieval_events))
        self.assertTrue(all(event["rights_record_ids"] == [] for event in plan.retrieval_events))
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)
        self.assertEqual({decision["status"] for decision in self.store.records("operation_decisions")}, {"unknown"})

    def test_repeat_inventory_has_no_new_events_or_artifacts(self) -> None:
        """Повторная сверка инвентарной регистрации возвращает пустой план."""

        plan, hashes, _ = self._reconcile()
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        before = self.fixture.snapshot()
        repeated, _, report = self._reconcile()

        self.assertEqual(repeated, ManifestPlan())
        self.assertEqual(report[0]["status"], "already_registered")
        self.assertEqual(self.fixture.snapshot(), before)

    def test_existing_success_is_not_replaced_with_recorded(self) -> None:
        """Тот же ответ переиспользует succeeded, хотя новый запрос использует artifact-контекст."""

        self._register_success(eligibility="eligible")
        before = self.fixture.snapshot()
        plan, _, report = self._reconcile()

        self.assertEqual(plan, ManifestPlan())
        self.assertEqual(report[0]["status"], "already_registered")
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertTrue(all(event["outcome"] == "succeeded" for event in self.store.records("retrieval_events")))
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "eligible")

    def test_same_bytes_with_changed_http_fields_remain_new_events(self) -> None:
        """URL, дата и метаданные ответа не подменяются одним совпавшим SHA файла."""

        self._register_success()

        for field, value in (
            ("retrieved_at", "2026-10-14T12:00:00+00:00"),
            ("requested_url", self.article.card_response.requested_url + "?new=1"),
            ("final_url", self.article.card_response.final_url + "?new=1"),
            ("headers", (("content-type", "text/html"), ("etag", "changed"))),
        ):
            with self.subTest(field=field):
                response = replace(self.article.card_response, **{field: value})
                article = replace(self.article, card_response=response)
                plan, _, report = self._reconcile([article])

                self.assertEqual(report, [])
                self.assertEqual(sum(event["outcome"] == "recorded" for event in plan.retrieval_events), 1)
                self.assertTrue(all(artifact["acquisition_status"] == "retrieved" for artifact in plan.artifacts))

    def test_new_response_preserves_existing_artifact_rights_and_status(self) -> None:
        """Другой HTTP-факт для прежнего PDF добавляет происхождение, не меняя его допуск."""

        self._register_success(eligibility="eligible")
        previous = {artifact["artifact_id"]: artifact for artifact in self.store.records("artifacts")}
        article = replace(self.article, pdf_response=replace(
            self.fixture.pdf, retrieved_at="2026-10-14T12:00:00+00:00",
        ))
        plan, hashes, report = self._reconcile([article])

        self.assertEqual(report, [])

        for artifact in plan.artifacts:
            for field in ("rights_record_ids", "acquisition_status", "qa_status", "processing_status"):
                self.assertEqual(artifact[field], previous[artifact["artifact_id"]][field])

        self.assertEqual(plan.works[0]["eligibility_status"], "eligible")
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertEqual(len(self.store.records("retrieval_events")), 3)
        self.assertTrue(self.store.audit().ok)

    def test_manual_pdf_preserves_its_method_rights_and_status(self) -> None:
        """Пилотный PDF ручной загрузки не получает права и статус нового crawler-факта."""

        rights = self.fixture.rights()
        rights[0].update({"acquisition_method": "manual_download", "acquisition_scope": "sample"})
        self.store.commit(ManifestPlan(rights=rights))
        options = RegistrationOptions(
            content_role="full_text", acquisition_method="manual_download", acquisition_scope="sample",
            rights_record_ids=RIGHTS_IDS, extraction_method="not_started", extraction_version="manual-test-v1",
            response_representation="pdf", request_context_type="work",
        )
        manual = plan_http_response(
            self.article.document, self.article.profile, options, REGISTERED_AT, self.fixture.pdf,
        )
        self.store.commit(manual)
        previous = self.store.records("artifacts")[0]
        article = replace(self.article, pdf_response=replace(
            self.fixture.pdf, retrieved_at="2026-10-14T12:00:00+00:00",
        ))
        plan, hashes, report = self._reconcile([article])

        self.assertEqual(report, [])
        pdf = next(artifact for artifact in plan.artifacts if artifact["representation"] == "pdf")

        for field in (
            "acquisition_method", "acquisition_scope", "acquisition_status", "rights_record_ids",
            "qa_status", "extraction_status", "processing_status",
        ):
            self.assertEqual(pdf[field], previous[field])

        self.assertEqual(len(pdf["retrievals"]), 2)
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)

    def test_equivalent_utc_time_reuses_complete_previous_projection(self) -> None:
        """Другой часовой пояс того же факта не создаёт событие и сохраняет прежнюю проекцию."""

        self.store.commit(ManifestPlan(rights=self.fixture.rights()))
        previous = plan_collected_article(self.article, rights_record_ids=RIGHTS_IDS, registered_at=REGISTERED_AT)
        event = previous.retrieval_events[0]
        event["retrieved_at"] = "2026-10-13T15:00:00+03:00"

        for artifact in previous.artifacts:
            for retrieval in artifact["retrievals"]:
                if retrieval["retrieval_id"] == event["retrieval_id"]:
                    retrieval["retrieved_at"] = event["retrieved_at"]

        self.store.commit(previous)
        plan, _, report = self._reconcile()

        self.assertEqual(plan, ManifestPlan())
        self.assertEqual(report[0]["status"], "already_registered")
        self.assertTrue(self.store.audit().ok)

    def test_existing_response_of_another_work_is_deferred(self) -> None:
        """Равные байты и HTTP-метаданные не разрешают присвоить событие другой статье."""

        self._register_success()
        document = copy.deepcopy(self.article.document)
        document.url = document.url.replace("p309", "p999")
        document.extra.pop("doi", None)
        document.extra["article_id"] = "r/170/4/p999"
        other = replace(self.article, document=document, pdf_response=None)
        plan, _, report = self._reconcile([other])

        self.assertEqual(plan, ManifestPlan())
        self.assertEqual(report[0]["status"], "deferred")
        self.assertIn("другой работой", report[0]["reason"])

    def test_reused_response_with_old_path_does_not_leave_orphan_blob(self) -> None:
        """Переиспользование прежнего пути HTTP-ответа не создаёт лишнюю копию без ссылок."""

        self.store.commit(ManifestPlan(rights=self.fixture.rights()))
        previous = plan_collected_article(self.article, rights_record_ids=RIGHTS_IDS, registered_at=REGISTERED_AT)
        old_path = previous.retrieval_events[0]["response_path"]
        preserved_path = "data/raw/older-import/article.html"
        previous.retrieval_events[0]["response_path"] = preserved_path
        previous.blobs = [
            replace(blob, relative_path=preserved_path) if blob.relative_path == old_path else blob
            for blob in previous.blobs
        ]

        for artifact in previous.artifacts:
            if artifact["path"] == old_path:
                artifact["path"] = preserved_path

        self.store.commit(previous)
        article = replace(self.article, pdf_response=replace(
            self.fixture.pdf, retrieved_at="2026-10-14T12:00:00+00:00",
        ))
        plan, hashes, report = self._reconcile([article])

        self.assertEqual(report, [])
        self.assertNotIn(old_path, {blob.relative_path for blob in plan.blobs})
        self.assertEqual(len(plan.blobs), 1)
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)

    def test_denied_controlling_right_is_kept_without_positive_fallback(self) -> None:
        """Конкретный запрет сохраняется вместо менее конкретного разрешения источника."""

        rights = self.fixture.rights()
        candidate = plan_collected_article(self.article, rights_record_ids=(), inventory_only=True)
        denied = copy.deepcopy(rights[0])
        denied.update({
            "rights_record_id": "work-denied", "scope_type": "work",
            "scope_id": candidate.works[0]["work_id"], "status": "prohibited",
        })
        self.store.commit(ManifestPlan(rights=[*rights, denied]))
        plan, hashes, report = self._reconcile()

        self.assertEqual(report, [])

        for event in plan.retrieval_events:
            self.assertIn("work-denied", event["rights_record_ids"])
            self.assertNotIn(RIGHTS_IDS[0], event["rights_record_ids"])

        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)
        self.assertIn("blocked", {decision["status"] for decision in self.store.records("operation_decisions")})

    def test_future_storage_right_is_not_attached_to_past_event(self) -> None:
        """Позднее основание хранения прикрепляется артефакту, но не переписывает HTTP-историю."""

        right = self.fixture.rights()[1]
        right["created_at"] = "2026-10-14T00:00:00+00:00"
        self.store.commit(ManifestPlan(rights=[right]))
        plan, hashes, _ = self._reconcile()

        self.assertTrue(all(event["rights_record_ids"] == [] for event in plan.retrieval_events))
        self.assertTrue(all(artifact["rights_record_ids"] == [RIGHTS_IDS[1]] for artifact in plan.artifacts))
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)

    def test_newly_known_retroactive_right_applies_to_past_response(self) -> None:
        """Время знания разрешения отделяется от указанного начала его действия."""

        rights = self.fixture.rights()

        for right in rights:
            right["created_at"] = "2026-10-14T00:00:00+00:00"
            right["effective_from"] = "2026-10-01T00:00:00+00:00"
            right["effective_from_evidence_sha256"] = "b" * 64

        self.store.commit(ManifestPlan(rights=rights))
        plan, hashes, _ = self._reconcile()

        self.assertTrue(all(set(event["rights_record_ids"]) == set(RIGHTS_IDS) for event in plan.retrieval_events))
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)

    def test_conflicting_title_is_deferred_without_eligibility_change(self) -> None:
        """Конфликт не превращает ранее согласованную работу в карантинную запись."""

        self._register_success(eligibility="eligible")
        article = copy.deepcopy(self.article)
        article.document.title = "Совсем другая публикация"
        before = self.fixture.snapshot()
        plan, _, report = self._reconcile([article])

        self.assertEqual(plan, ManifestPlan())
        self.assertEqual(report[0]["status"], "deferred")
        self.assertIn("title", report[0]["reason"])
        self.assertEqual(report[0]["path"], str(article.record_path))
        self.assertEqual(self.fixture.snapshot(), before)

    def test_ambiguous_same_work_in_batch_is_deferred(self) -> None:
        """Несколько версий одной работы требуют выбора до атомарной записи партии."""

        other = copy.deepcopy(self.article)
        other.document.title = "Другое название при прежнем DOI"
        plan, _, report = self._reconcile([self.article, other])

        self.assertEqual(plan, ManifestPlan())
        self.assertEqual([item["status"] for item in report], ["deferred", "deferred"])

    def test_exact_duplicate_in_batch_is_skipped(self) -> None:
        """Полностью одинаковая карточка в партии учитывается однократно."""

        plan, _, report = self._reconcile([self.article, self.article])

        self.assertEqual(len(plan.works), 1)
        self.assertEqual(len(plan.retrieval_events), 2)
        self.assertEqual(report[0]["status"], "skipped")

    def test_shared_source_response_does_not_conflict_between_works(self) -> None:
        """Два материала общего оглавления используют одно событие уровня источника."""

        issue_url = "https://quantum-electronics.ru/2024_54_1/"
        response = replace(self.fixture.card, requested_url=issue_url, final_url=issue_url)
        articles = []

        for index in (1, 2):
            document = Document(
                source="quantum-electronics.ru",
                url=f"https://quantum-electronics.ru/wp-content/uploads/2024/01/{index:04d}.pdf",
                title=f"Синтетическая физическая публикация {index}", text="",
                extra={"year": 2024, "metadata_source": "issue_toc", "issue_url": issue_url},
            )
            articles.append(CollectedArticle(
                document, get_source_profile("quantum_electronics"), response, None,
                self.fixture.record_path.with_name(f"quantum-{index}.json"),
            ))

        plan, hashes, report = self._reconcile(articles)

        self.assertEqual(report, [])
        self.assertEqual(len(plan.works), 2)
        self.assertEqual(len(plan.retrieval_events), 1)
        self.assertEqual(plan.artifacts, [])
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        self.assertTrue(self.store.audit().ok)

    def test_concurrent_snapshot_change_aborts_preparation(self) -> None:
        """Изменение реестра во время чтения не возвращает план со случайным CAS."""

        original = self.store.snapshot_hashes()

        with (
            patch.object(self.store, "snapshot_hashes", side_effect=[original, {**original, "works": "changed"}]),
            self.assertRaises(ManifestConcurrencyError),
        ):
            self._reconcile()

    def test_invalid_registration_time_is_rejected(self) -> None:
        """Время регистрации обязательно содержит часовой пояс."""

        with self.assertRaises(ValueError):
            reconcile_inventory_registration(self.store, [self.article], "2026-10-14T13:00:00")

    def test_empty_batch_is_read_only(self) -> None:
        """Пустая партия возвращает пустой план и действующий снимок без записи."""

        before = self.fixture.snapshot()
        plan, hashes, report = self._reconcile([])

        self.assertEqual(plan, ManifestPlan())
        self.assertEqual(hashes, self.store.snapshot_hashes())
        self.assertEqual(report, [])
        self.assertEqual(self.fixture.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
