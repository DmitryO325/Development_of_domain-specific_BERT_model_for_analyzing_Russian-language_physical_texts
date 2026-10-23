"""Проверки офлайн-регистрации собранных журналов без создания текста и допуска."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pymupdf

from src.collect.base import Document, HttpResponseSnapshot
from src.collect.ioffe_archive import IoffeArchive
from src.collect.jetp import parse_article
from src.collect.journal_sources import get_source
from src.corpus.journal_import import CollectedArticle, read_collected_article
from src.corpus.journal_registration import combine_article_plans, load_articles, plan_collected_article
from src.corpus.manifests import ManifestConflictError, ManifestPlan, ManifestStore, PlannedBlob
from src.corpus.profiles import get_source_profile
from src.corpus.registration import reconcile_document_plan
from tests.test_jetp import ARTICLE_HTML, ARTICLE_URL, PDF_URL

ROOT = Path(__file__).resolve().parents[1]
RETRIEVED_AT = "2026-10-13T12:00:00+00:00"
RIGHTS_IDS = ("test-journal-acquisition", "test-journal-storage")


class JournalFixture:
    """Изолированная карточка с точными исходниками и исключительно тестовыми правами."""

    def __init__(self, root: Path, *, with_pdf: bool = True) -> None:
        """Сохранить небольшие исходники в формате настоящего сборщика без обращения к сети."""

        root = root.resolve()
        self.root = root
        self.raw_root = root / "raw"
        self.input_dir = root / "imports"
        self.manifest_dir = root / "manifests"
        source = get_source("jetp")
        self.archive = IoffeArchive(
            self.raw_root, self.input_dir, MagicMock(),
            validate_url=source.validate_url, accepted_statuses=source.accepted_statuses,
        )
        self.document = parse_article(ARTICLE_HTML, ARTICLE_URL)
        self.card = HttpResponseSnapshot(
            ARTICLE_URL, ARTICLE_URL, 200,
            (("content-type", "text/html; charset=utf-8"),),
            RETRIEVED_AT, ARTICLE_HTML.encode("utf-8"),
        )

        with pymupdf.open() as document:
            document.new_page().insert_text((30, 30), "Synthetic article, not a real publication")
            pdf_bytes = document.tobytes()

        self.pdf = HttpResponseSnapshot(
            PDF_URL, PDF_URL, 200, (("content-type", "application/pdf"),),
            "2026-10-13T12:01:00+00:00", pdf_bytes,
        )
        self.archive.import_response(self.card)
        self.record: dict[str, Any] = {
            "schema_version": "journal-collected-article-v1",
            "source_id": "jetp",
            "document": self.document.to_dict(),
            "metadata_page_url": ARTICLE_URL,
            "card_response": self.archive.evidence_for(self.card),
            "status": "pdf_downloaded" if with_pdf else "metadata_only",
            "registered": False,
            "training_eligibility": "not_assessed",
        }

        if with_pdf:
            self.archive.import_response(self.pdf)
            self.record["pdf_response"] = self.archive.evidence_for(self.pdf)
            self.record["page_count"] = 1

        directory = "articles" if with_pdf else "metadata"
        self.record_path = self.input_dir / directory / "article.json"
        self.save_record()

    def save_record(self) -> None:
        """Обновить только синтетическую карточку тестового каталога."""

        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self.record_path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")

    def article(self) -> CollectedArticle:
        """Прочитать карточку тем же проверяющим кодом, что используется командой."""

        return read_collected_article(self.record_path, source="jetp", raw_root=self.raw_root)

    def store(self) -> ManifestStore:
        """Открыть только временное хранилище с рабочими схемами проекта."""

        return ManifestStore(
            project_root=self.root, manifest_dir=self.manifest_dir,
            schema_dir=ROOT / "manifests/schemas",
        )

    def rights(self, *, status: str = "allowed") -> list[dict[str, Any]]:
        """Создать два синтетических права; эти записи никогда не попадают в рабочий корпус."""

        return [{
            "schema_version": "rights-v1",
            "created_at": "2026-09-01T00:00:00+00:00",
            "rights_record_id": f"test-journal-{operation}",
            "scope_type": "source_group",
            "scope_id": self.article().profile.source_group_id,
            "operation": operation,
            "status": status,
            "access_basis": "Исключительно синтетическое разрешение для временного теста.",
            "basis_type": "explicit_license",
            "acquisition_method": "crawler" if operation == "acquisition" else None,
            "acquisition_scope": "bulk" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2026-09-01",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        } for operation in ("acquisition", "storage")]

    def snapshot(self) -> dict[str, bytes]:
        """Снять точные байты временного проекта для проверки отсутствия записи."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


class JournalRegistrationTests(unittest.TestCase):
    """Проверки метаданных, происхождения и повторного атомарного импорта."""

    def setUp(self) -> None:
        """Подготовить независимую карточку с PDF и неизменяемыми HTTP-снимками."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.fixture = JournalFixture(Path(temporary_directory.name))

    def _plan(self) -> ManifestPlan:
        """Сформировать один план с явным списком тестовых прав."""

        return plan_collected_article(self.fixture.article(), rights_record_ids=RIGHTS_IDS)

    def test_metadata_and_original_pdf_do_not_create_extracted_text(self) -> None:
        """Аннотация остаётся метаданными, а исходный PDF не становится результатом OCR."""

        before = self.fixture.snapshot()
        plan = self._plan()
        work = plan.works[0]
        artifacts = {artifact["representation"]: artifact for artifact in plan.artifacts}

        self.assertEqual(len(plan.works), 1)
        self.assertEqual(set(artifacts), {"html", "pdf"})
        self.assertEqual(artifacts["html"]["content_role"], "metadata_only")
        self.assertEqual(artifacts["pdf"]["content_role"], "full_text")
        self.assertEqual(work["title"], self.fixture.document.title)
        self.assertEqual(work["abstract"], self.fixture.document.text)
        self.assertEqual(work["authors"], self.fixture.document.authors)
        self.assertEqual(work["published_year"], 2026)
        self.assertIsNone(work["published_at"])
        self.assertEqual(work["eligibility_status"], "pending")
        self.assertFalse(plan.rights)
        self.assertFalse(plan.operation_decisions)
        self.assertFalse(any(blob.relative_path.endswith(".txt") for blob in plan.blobs))

        for artifact in plan.artifacts:
            self.assertEqual(artifact["extraction_status"], "not_started")
            self.assertEqual(artifact["qa_status"], "not_evaluated")
            self.assertIsNone(artifact["ocr_method"])

        self.assertEqual(self.fixture.snapshot(), before)

    def test_http_events_pin_exact_body_and_http_metadata_hashes(self) -> None:
        """Каждое событие соответствует исходному ответу, а не JSON разобранной карточки."""

        plan = self._plan()
        events = {event["requested_url"]: event for event in plan.retrieval_events}

        self.assertEqual(set(events), {ARTICLE_URL, PDF_URL})

        for snapshot in (self.fixture.card, self.fixture.pdf):
            event = events[snapshot.requested_url]
            self.assertEqual(event["response_sha256"], hashlib.sha256(snapshot.body).hexdigest())
            self.assertEqual(event["response_metadata_sha256"], snapshot.metadata_sha256())
            self.assertEqual(event["acquisition_method"], "crawler")
            self.assertEqual(event["acquisition_scope"], "bulk")
            self.assertEqual(set(event["rights_record_ids"]), set(RIGHTS_IDS))

    def test_metadata_only_has_no_pdf_artifact_or_empty_text(self) -> None:
        """Карточка без PDF создаёт один HTML-артефакт и сохраняет аннотацию."""

        article = replace(self.fixture.article(), pdf_response=None)
        plan = plan_collected_article(article, rights_record_ids=RIGHTS_IDS)

        self.assertEqual([artifact["representation"] for artifact in plan.artifacts], ["html"])
        self.assertEqual(len(plan.retrieval_events), 1)
        self.assertEqual(plan.works[0]["abstract"], article.document.text)
        self.assertEqual(plan.works[0]["eligibility_status"], "pending")

    def test_explicit_rights_are_required_before_building_plan(self) -> None:
        """Пустой список прав не заменяется автоматически созданным разрешением."""

        before = self.fixture.snapshot()

        with self.assertRaises(ValueError):
            plan_collected_article(self.fixture.article(), rights_record_ids=())

        self.assertEqual(self.fixture.snapshot(), before)

    def test_repeated_registration_is_idempotent_and_preserves_pending(self) -> None:
        """Повторный импорт тех же исходников не меняет записи, файлы и допуск."""

        store = self.fixture.store()
        store.commit(ManifestPlan(rights=self.fixture.rights()))
        first, first_hashes = reconcile_document_plan(store, self._plan())
        first_result = store.commit(first, expected_snapshot_hashes=first_hashes)
        before = self.fixture.snapshot()
        repeated, repeated_hashes = reconcile_document_plan(store, self._plan())
        repeated_result = store.commit(repeated, expected_snapshot_hashes=repeated_hashes)

        self.assertGreater(sum(first_result.inserted.values()), 0)
        self.assertEqual(sum(repeated_result.inserted.values()), 0)
        self.assertEqual(sum(repeated_result.updated.values()), 0)
        self.assertEqual(repeated_result.written_blobs, 0)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(store.records("works")[0]["eligibility_status"], "pending")
        self.assertLessEqual(
            {decision["operation"] for decision in store.records("operation_decisions")},
            {"acquisition", "storage"},
        )
        self.assertTrue(store.audit().ok)

    def test_dry_run_leaves_existing_files_unchanged(self) -> None:
        """Проверка плана не записывает ни новые объекты, ни реестровые строки."""

        store = self.fixture.store()
        store.commit(ManifestPlan(rights=self.fixture.rights()))
        before = self.fixture.snapshot()
        plan, hashes = reconcile_document_plan(store, self._plan())
        result = store.commit(plan, dry_run=True, expected_snapshot_hashes=hashes)

        self.assertTrue(result.dry_run)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(store.records("works"), [])

    def test_metadata_then_pdf_adds_only_missing_pdf_and_event(self) -> None:
        """Позднее получение PDF не дублирует уже зарегистрированные HTML и работу."""

        store = self.fixture.store()
        store.commit(ManifestPlan(rights=self.fixture.rights()))
        article = self.fixture.article()
        metadata_plan = plan_collected_article(replace(article, pdf_response=None), rights_record_ids=RIGHTS_IDS)
        initial, initial_hashes = reconcile_document_plan(store, metadata_plan)
        store.commit(initial, expected_snapshot_hashes=initial_hashes)
        initial_work_id = store.records("works")[0]["work_id"]
        initial_html_id = store.records("artifacts")[0]["artifact_id"]
        full, full_hashes = reconcile_document_plan(store, self._plan())
        result = store.commit(full, expected_snapshot_hashes=full_hashes)

        self.assertEqual(result.inserted["works"], 0)
        self.assertEqual(result.inserted["artifacts"], 1)
        self.assertEqual(result.inserted["retrieval_events"], 1)
        self.assertEqual(len(store.records("works")), 1)
        self.assertEqual(store.records("works")[0]["work_id"], initial_work_id)
        self.assertIn(initial_html_id, {artifact["artifact_id"] for artifact in store.records("artifacts")})
        self.assertEqual(store.records("works")[0]["eligibility_status"], "pending")
        self.assertEqual(len(store.records("artifacts")), 2)

    def test_existing_eligibility_is_not_replaced_by_import_default(self) -> None:
        """Прежний согласованный допуск не сбрасывается в pending повторным сбором."""

        store = self.fixture.store()
        store.commit(ManifestPlan(rights=self.fixture.rights()))
        initial = self._plan()
        initial.works[0].update({
            "eligibility_status": "eligible", "genre": "research_article",
            "published_at": "2026-10-01",
        })
        store.commit(initial)
        repeated, hashes = reconcile_document_plan(store, self._plan())
        store.commit(repeated, expected_snapshot_hashes=hashes)

        self.assertEqual(store.records("works")[0]["eligibility_status"], "eligible")
        self.assertEqual(store.records("works")[0]["published_at"], "2026-10-01")

    def test_shared_issue_html_is_source_evidence_not_two_owned_artifacts(self) -> None:
        """Общее оглавление связывает две статьи без присвоения одного HTML разным работам."""

        issue_url = "https://quantum-electronics.ru/2024_54_1/"
        issue = replace(self.fixture.card, requested_url=issue_url, final_url=issue_url)
        profile = get_source_profile("quantum_electronics")
        plans = []

        for index in (1, 2):
            pdf_url = f"https://quantum-electronics.ru/wp-content/uploads/2024/01/{index:04d}.pdf"
            document = Document(
                source="quantum-electronics.ru", url=pdf_url,
                title=f"Синтетическая физическая статья {index}", text="",
                pdf_url=pdf_url, extra={"year": 2024, "metadata_source": "issue_toc", "issue_url": issue_url},
            )
            pdf = replace(self.fixture.pdf, requested_url=pdf_url, final_url=pdf_url,
                          body=self.fixture.pdf.body + f"\n% {index}\n".encode())
            article = CollectedArticle(document, profile, issue, pdf, self.fixture.record_path)
            plans.append(plan_collected_article(
                article, rights_record_ids=RIGHTS_IDS, registered_at="2026-10-13T13:00:00+00:00",
            ))

        combined = combine_article_plans(plans)

        self.assertEqual(len(combined.works), 2)
        self.assertEqual([artifact["representation"] for artifact in combined.artifacts], ["pdf", "pdf"])
        self.assertEqual(len(combined.retrieval_events), 3)
        source_events = [event for event in combined.retrieval_events if event["request_context_type"] == "source"]
        self.assertEqual(len(source_events), 1)
        self.assertEqual(source_events[0]["requested_url"], issue_url)

    def test_combination_deduplicates_exact_same_plan_without_mutating_input(self) -> None:
        """Точный повтор партии объединяется без повторов и изменения переданного плана."""

        plan = self._plan()
        original = copy.deepcopy(plan)
        combined = combine_article_plans([plan, copy.deepcopy(plan)])

        self.assertEqual(combined.works, plan.works)
        self.assertEqual(combined.artifacts, plan.artifacts)
        self.assertEqual(combined.retrieval_events, plan.retrieval_events)
        self.assertEqual(combined.blobs, plan.blobs)
        self.assertEqual(plan, original)

    def test_same_work_identifier_with_conflicting_title_is_rejected(self) -> None:
        """Разные статьи с одинаковым устойчивым ID не склеиваются молча."""

        first = self._plan()
        second = copy.deepcopy(first)
        second.works[0]["title"] = "Несовместимое название другой статьи"

        with self.assertRaises((ValueError, ManifestConflictError)):
            combine_article_plans([first, second])

    def test_same_artifact_identifier_for_different_work_is_rejected(self) -> None:
        """Один PDF не приписывается второй независимой статье в обход проверки идентичности."""

        first = self._plan()
        second = copy.deepcopy(first)
        second.artifacts[0]["work_id"] = "00000000-0000-5000-8000-000000000001"

        with self.assertRaises((ValueError, ManifestConflictError)):
            combine_article_plans([first, second])

    def test_same_blob_path_with_changed_bytes_is_rejected(self) -> None:
        """Совпадение пути объекта не разрешает заменить уже запланированные байты."""

        first = self._plan()
        second = copy.deepcopy(first)
        original = second.blobs[0]
        second.blobs[0] = PlannedBlob(original.relative_path, b"changed", original.sha256)

        with self.assertRaises((ValueError, ManifestConflictError)):
            combine_article_plans([first, second])

    def test_empty_combination_is_an_empty_plan(self) -> None:
        """Пустая партия не создаёт синтетические работы или события."""

        self.assertEqual(combine_article_plans([]), ManifestPlan())

    def test_loader_reads_completed_article_from_collection_directory(self) -> None:
        """Каталог сборщика читается автономно с проверкой исходников каждой карточки."""

        before = self.fixture.snapshot()
        articles = load_articles(self.fixture.input_dir, source="jetp", raw_root=self.fixture.raw_root)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].document.to_dict(), self.fixture.document.to_dict())
        self.assertEqual(self.fixture.snapshot(), before)

    def test_loader_prefers_full_pdf_record_without_discarding_metadata_validation(self) -> None:
        """Два согласованных режима одной карточки дают одну работу с PDF даже при лимите."""

        metadata_record = copy.deepcopy(self.fixture.record)
        metadata_record.pop("pdf_response")
        metadata_record.pop("page_count")
        metadata_record["status"] = "metadata_only"
        metadata_path = self.fixture.input_dir / "metadata" / "same-article.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(json.dumps(metadata_record), encoding="utf-8")

        articles = load_articles(self.fixture.input_dir, source="jetp", raw_root=self.fixture.raw_root,
                                 max_articles=1)

        self.assertEqual(len(articles), 1)
        self.assertIsNotNone(articles[0].pdf_response)

    def test_loader_does_not_resolve_inconsistent_duplicate_by_taking_first(self) -> None:
        """Отличающиеся метаданные дубликата останавливают всю партию, а не скрываются."""

        altered = copy.deepcopy(self.fixture.record)
        altered["document"]["title"] = "Другая статья"
        metadata_path = self.fixture.input_dir / "metadata" / "same-article.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(json.dumps(altered), encoding="utf-8")
        before = self.fixture.snapshot()

        with self.assertRaises(ValueError):
            load_articles(self.fixture.input_dir, source="jetp", raw_root=self.fixture.raw_root,
                          max_articles=1)

        self.assertEqual(self.fixture.snapshot(), before)

    def test_loader_rejects_changed_raw_body_before_any_plan_is_committed(self) -> None:
        """Повреждённое тело PDF не проходит за счёт наличия прежнего JSON-свидетельства."""

        pdf_path = self.fixture.raw_root / self.fixture.record["pdf_response"]["relative_path"]
        pdf_path.write_bytes(b"changed")
        before = self.fixture.snapshot()

        with self.assertRaises(ValueError):
            load_articles(self.fixture.input_dir, source="jetp", raw_root=self.fixture.raw_root)

        self.assertEqual(self.fixture.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
