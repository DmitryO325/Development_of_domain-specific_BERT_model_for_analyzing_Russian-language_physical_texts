"""Проверки семантики и неизменяемой офлайн-регистрации журнальных статей ОИЯИ."""

from __future__ import annotations

import copy
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.jinr_article_input import (
    ADJACENT_COMPUTATIONAL_METHODS_GROUP,
    ADJACENT_COMPUTING_GROUP,
    ArticleBatch,
    read_article_batch,
)
from src.corpus.jinr_article_registration import (
    plan_article_batch,
    reconcile_article_plan,
)
from src.corpus.manifests import (
    ManifestConflictError,
    ManifestError,
    ManifestPlan,
    ManifestStore,
)
from src.corpus.registration import RegistrationOptions
from src.corpus.schema_validation import SchemaCatalog
from tests.jinr_article_fixtures import (
    ITEM_UUID,
    RETRIEVED_AT,
    SEARCH_URL,
    ArticleFixture,
)

ROOT = Path(__file__).resolve().parents[1]
COLLECTED_AT = "2026-09-29T12:00:00+00:00"
LATER_COLLECTED_AT = "2026-09-30T12:00:00+00:00"
METADATA_RIGHTS = ("right-api", "right-storage")


class JinrArticleRegistrationTests(unittest.TestCase):
    """Проверки реальных метаданных, ручного происхождения и безопасной записи плана."""

    def setUp(self) -> None:
        """Создать синтетическую статью с двумя историями получения одного PDF."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.fixture = ArticleFixture(self.root, aliases=2)
        self.fixture.entry["genre"] = "research_article"
        self.fixture.entry["journal_id"] = "test_physics"
        self.fixture.item["metadata"]["local.EDN"] = [{"value": "ABCDEF"}]
        self.fixture.save_metadata()
        self.options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="bulk",
            rights_record_ids=("right-manual_download", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )

    def _plan(self, collected_at: str = COLLECTED_AT) -> ManifestPlan:
        """Построить план с раздельными явными правами на API и PDF."""

        return plan_article_batch(
            self.root,
            self.fixture.path,
            self.options,
            collected_at=collected_at,
            metadata_rights_record_ids=METADATA_RIGHTS,
        )

    def _store(self) -> ManifestStore:
        """Использовать рабочие схемы только для отдельного временного реестра."""

        return ManifestStore(
            project_root=self.root,
            manifest_dir=self.root / "manifests",
            schema_dir=ROOT / "manifests/schemas",
        )

    def _file_bytes(self) -> dict[str, bytes]:
        """Снять точное содержимое всех файлов тестового каталога."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def _right(self, name: str) -> dict[str, Any]:
        """Создать разрешение исключительно для синтетического интеграционного теста."""

        acquisition = name != "storage"

        return {
            "schema_version": "rights-v1",
            "created_at": "2026-09-01T00:00:00+00:00",
            "rights_record_id": f"right-{name}",
            "scope_type": "source_group",
            "scope_id": "F04_JINR_REPOSITORY",
            "operation": "acquisition" if acquisition else "storage",
            "status": "allowed",
            "access_basis": "Исключительно синтетическое право в тесте.",
            "basis_type": "explicit_license",
            "acquisition_method": name if acquisition else None,
            "acquisition_scope": "bulk" if acquisition else None,
            "terms_url": "https://example.invalid/license",
            "rights_checked_at": "2026-09-01",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def _plan_with_test_rights(self) -> ManifestPlan:
        """Добавить тестовые разрешения, которые регистратор сам не создаёт."""

        plan = self._plan()
        plan.rights = [self._right(name) for name in ("api", "manual_download", "storage")]

        return plan

    def _verified_pair(self, *, same_doi: bool = False) -> ArticleBatch:
        """Подготовить пару карточек для проверки сборщика после границы чтения."""

        batch = read_article_batch(self.root, self.fixture.path)
        first = batch.entries[0]
        second_entry = copy.deepcopy(first.entry)
        second_item = copy.deepcopy(first.item)
        second_uuid = "c1b898f8-e68c-4aed-8d11-3f48001dbaaa"
        second_entry["item_uuid"] = second_uuid
        second_item["uuid"] = second_uuid
        second_item["id"] = second_uuid
        second_item["_links"]["self"]["href"] = (
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{second_uuid}"
        )
        second_item["metadata"]["dc.title"] = [{"value": "Другая физическая публикация"}]
        second_item["metadata"].pop("local.EDN")

        if not same_doi:
            second_item["metadata"]["dc.identifier.doi"] = [{"value": "10.1000/second"}]

        # Здесь проверяется сборка плана из проверенных карточек; чтение описи
        # отдельно покрывается реальными файлами во всех интеграционных проверках.
        second = replace(
            first, entry=second_entry, item=second_item,
            pdf_sha256="b" * 64, pdf_path="data/raw/pdf/manual/second.pdf",
        )

        return replace(batch, entries=(first, second))

    def test_plan_preserves_article_journal_edn_and_year(self) -> None:
        """Article остаётся журнальной статьёй без выдуманной даты и допуска к обучению."""

        plan = self._plan()
        work = plan.works[0]

        self.assertEqual(len(plan.works), 1)
        self.assertEqual(work["source_id"], "F04_JINR_ARTICLES_RU")
        self.assertEqual(work["source_group_id"], "F04_JINR_REPOSITORY")
        self.assertEqual(work["genre"], "research_article")
        self.assertEqual(work["journal_id"], "test_physics")
        self.assertEqual(work["journal_title"], "Тестовый физический журнал")
        self.assertEqual(work["title"], "Нейтронные свойства вещества")
        self.assertEqual(work["authors"], ["Иванов И. И."])
        self.assertEqual(work["abstract"], "Аннотация статьи.")
        self.assertEqual(work["keywords"], ["нейтроны"])
        self.assertEqual(work["doi"], "10.1000/article")
        self.assertEqual(work["edn"], "ABCDEF")
        self.assertEqual(work["published_year"], 2024)
        self.assertIsNone(work["published_at"])
        self.assertEqual(work["eligibility_status"], "pending")
        self.assertIsNone(work["exclusion_reason"])
        self.assertEqual(work["canonical_url"], self.fixture.item["_links"]["self"]["href"])
        self.assertEqual(plan.rights, [])
        self.assertEqual(plan.blobs, [])
        self.assertEqual(plan.operation_decisions, [])
        self.assertTrue(plan.warnings)
        self.assertNotIn("corpus_group_id", work)

    def test_bilingual_title_and_adjacent_group_preserve_raw_provenance(self) -> None:
        """План выбирает русский оригинал и сохраняет переводы в проверенном API-свидетельстве."""

        russian_title = "Нейтронные свойства\nвещества"
        titles = [
            {"value": "Neutron properties of matter", "language": None, "place": 0},
            {"value": russian_title, "language": None, "place": 1},
        ]
        self.fixture.item["metadata"]["dc.title"] = copy.deepcopy(titles)
        self.fixture.save_metadata(sync_copies=False)
        self.fixture.entry.update({
            "title": russian_title, "titles_as_reported": copy.deepcopy(titles),
            "corpus_group_id": ADJACENT_COMPUTING_GROUP,
        })
        self.fixture.selection[0].update({"title": russian_title, "corpus_group_id": ADJACENT_COMPUTING_GROUP})

        for match in self.fixture.matches:
            match["matched_item"]["title"] = russian_title

        self.fixture.save_sources()
        before = self._file_bytes()
        plan = self._plan()
        work = plan.works[0]
        api_event = next(event for event in plan.retrieval_events if event["acquisition_method"] == "api")

        self.assertEqual(work["title"], russian_title)
        self.assertEqual(work["corpus_group_id"], ADJACENT_COMPUTING_GROUP)
        self.assertEqual(work["eligibility_status"], "pending")
        self.assertEqual(work["source_id"], "F04_JINR_ARTICLES_RU")
        self.assertEqual(work["genre"], "research_article")
        self.assertEqual(api_event["response_sha256"], self.fixture.entry["metadata_source"]["api_response_sha256"])
        self.assertEqual(read_article_batch(self.root, self.fixture.path).entries[0].item["metadata"]["dc.title"], titles)
        self.assertEqual(self._file_bytes(), before)

    def test_unknown_corpus_group_is_rejected_before_registration(self) -> None:
        """Согласованные, но неизвестные группы не разрешают создание работы."""

        self.fixture.entry["corpus_group_id"] = "unapproved_group"
        self.fixture.selection[0]["corpus_group_id"] = "unapproved_group"
        self.fixture.save_sources()
        before = self._file_bytes()

        with self.assertRaisesRegex(ValueError, "corpus_group_id"):
            self._plan()

        self.assertEqual(self._file_bytes(), before)

    def _assert_group_registration(self, group: str) -> None:
        """Проверить сохранение явной группы в плане, реестре и неизменяемом повторе."""

        self.fixture.entry["corpus_group_id"] = group
        self.fixture.selection[0]["corpus_group_id"] = group
        self.fixture.save_sources()
        store = self._store()
        plan = self._plan_with_test_rights()

        self.assertEqual(plan.works[0]["corpus_group_id"], group)
        self.assertEqual(plan.works[0]["eligibility_status"], "pending")
        self.assertEqual(plan.works[0]["genre"], "research_article")

        store.preflight([copy.deepcopy(plan)])
        result = store.commit(plan)
        committed_bytes = self._file_bytes()
        repeated, expected_hashes = reconcile_article_plan(store, self._plan(LATER_COLLECTED_AT))

        self.assertEqual(repeated.works[0]["corpus_group_id"], group)
        store.preflight([repeated], expected_snapshot_hashes=expected_hashes)
        repeated_result = store.commit(repeated, expected_snapshot_hashes=expected_hashes)

        self.assertEqual(result.inserted["works"], 1)
        self.assertEqual(store.records("works")[0]["corpus_group_id"], group)
        self.assertEqual(store.records("works")[0]["eligibility_status"], "pending")
        self.assertEqual(sum(repeated_result.inserted.values()), 0)
        self.assertEqual(sum(repeated_result.updated.values()), 0)
        self.assertEqual(self._file_bytes(), committed_bytes)
        self.assertTrue(store.audit().ok)

    def test_experimental_computing_group_survives_commit_and_repeat(self) -> None:
        """Ранее одобренная группа сохраняет совместимость после добавления новой."""

        self._assert_group_registration(ADJACENT_COMPUTING_GROUP)

    def test_computational_methods_group_survives_commit_and_repeat(self) -> None:
        """Вычислительные методы учитываются отдельно и не получают допуска к обучению."""

        self._assert_group_registration(ADJACENT_COMPUTATIONAL_METHODS_GROUP)

    def test_manual_downloads_preserve_two_histories_and_one_artifact(self) -> None:
        """Два ручных получения одного PDF сохраняются без выдуманных HTTP-ответов."""

        plan = self._plan()
        manual_events = [event for event in plan.retrieval_events if event["acquisition_method"] == "manual_download"]
        metadata_events = [event for event in plan.retrieval_events if event["acquisition_method"] == "api"]
        artifact = plan.artifacts[0]

        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(manual_events), 2)
        self.assertEqual(len({event["retrieval_id"] for event in manual_events}), 2)
        self.assertEqual(len(artifact["retrievals"]), 2)
        self.assertEqual(artifact["path"], self.fixture.entry["pdf"]["path"])
        self.assertEqual(artifact["sha256"], self.fixture.entry["pdf"]["sha256"])
        self.assertEqual(artifact["bytes"], len(self.fixture.pdf_bytes))
        self.assertEqual(artifact["content_role"], "full_text")
        self.assertEqual(artifact["representation"], "pdf")
        self.assertEqual(artifact["acquisition_method"], "manual_download")
        self.assertEqual(artifact["acquisition_scope"], "bulk")
        self.assertEqual(artifact["extraction_status"], "not_started")
        self.assertEqual(artifact["qa_status"], "not_evaluated")

        for event, acquisition in zip(manual_events, self.fixture.inventory, strict=True):
            self.assertEqual(event["requested_url"], acquisition["source_url"])
            self.assertEqual(event["retrieved_at"], acquisition["retrieved_at"])
            self.assertEqual(event["acquisition_agent"], acquisition["acquisition_agent"])
            self.assertEqual(event["response_path"], acquisition["staged_relative_path"])
            self.assertEqual(event["request_context_id"], plan.works[0]["work_id"])
            self.assertEqual(event["rights_record_ids"], list(self.options.rights_record_ids))
            self.assertIsNone(event["http_status"])
            self.assertIsNone(event["final_url"])
            self.assertEqual(event["response_headers"], {})

        self.assertEqual(len(metadata_events), 1)
        metadata_event = metadata_events[0]
        self.assertEqual(metadata_event["http_status"], 200)
        self.assertEqual(metadata_event["requested_url"], SEARCH_URL)
        self.assertEqual(metadata_event["final_url"], SEARCH_URL)
        self.assertEqual(metadata_event["retrieved_at"], RETRIEVED_AT)
        self.assertEqual(metadata_event["request_context_type"], "source")
        self.assertEqual(metadata_event["request_context_id"], "F04_JINR_ARTICLES_RU")
        self.assertEqual(metadata_event["rights_record_ids"], list(METADATA_RIGHTS))
        self.assertEqual(metadata_event["response_headers"]["content-type"], "application/json")
        self.assertEqual(
            {alias["alias_type"]: alias["alias_value"] for alias in plan.work_aliases},
            {"source_native_id": f"F04_JINR_ARTICLES_RU:{ITEM_UUID}", "doi": "10.1000/article", "edn": "ABCDEF"},
        )

        for alias in plan.work_aliases:
            self.assertEqual(alias["source_retrieval_id"], metadata_event["retrieval_id"])
            self.assertEqual(alias["evidence_sha256"], metadata_event["response_sha256"])

    def test_full_date_and_explicit_subgenre_are_preserved(self) -> None:
        """Точная дата и явно назначенный поджанр не заменяются значениями по умолчанию."""

        self.fixture.entry["genre"] = "review_article"
        self.fixture.item["metadata"]["dc.date.issued"] = [{"value": "2024-05-28"}]
        self.fixture.save_metadata()
        work = self._plan().works[0]

        self.assertEqual(work["genre"], "review_article")
        self.assertEqual(work["published_at"], "2024-05-28")
        self.assertEqual(work["published_year"], 2024)

    def test_missing_edn_is_not_invented(self) -> None:
        """Отсутствующий в API EDN остаётся незаполненным."""

        self.fixture.item["metadata"].pop("local.EDN")
        self.fixture.save_metadata()
        plan = self._plan()

        self.assertIsNone(plan.works[0]["edn"])
        self.assertNotIn("edn", {alias["alias_type"] for alias in plan.work_aliases})
        self.assertFalse(any("EDN" in warning for warning in plan.warnings))

    def test_single_edn_and_case_duplicates_are_normalized(self) -> None:
        """Один EDN и его дубликаты с разным регистром дают один нормализованный код."""

        for values in ((" lfzhvr ",), ("lfzhvr", "LFZHVR", " LfZhVr ")):
            with self.subTest(values=values):
                self.fixture.item["metadata"]["local.EDN"] = [{"value": value} for value in values]
                self.fixture.save_metadata()
                plan = self._plan()

                self.assertEqual(plan.works[0]["edn"], "LFZHVR")
                self.assertEqual(
                    [alias["alias_value"] for alias in plan.work_aliases if alias["alias_type"] == "edn"],
                    ["LFZHVR"],
                )
                self.assertFalse(any("EDN" in warning for warning in plan.warnings))

    def test_ambiguous_edn_preserves_primary_identity_and_warns(self) -> None:
        """Несколько корректных EDN оставляют DOI и UUID без выбора случайного псевдонима."""

        expected_work_id = self._plan().works[0]["work_id"]

        for values in (("lfzhvr", "iewszd"), ("lfzhvr", "iewszd", "ABCDEF")):
            with self.subTest(values=values):
                self.fixture.item["metadata"]["local.EDN"] = [{"value": value} for value in values]
                self.fixture.save_metadata()
                before = self._file_bytes()
                plan = self._plan()
                work = plan.works[0]
                edn_warnings = [warning for warning in plan.warnings if "EDN" in warning]

                self.assertIsNone(work["edn"])
                self.assertEqual(work["work_id"], expected_work_id)
                self.assertEqual(work["doi"], "10.1000/article")
                self.assertEqual(
                    {alias["alias_type"]: alias["alias_value"] for alias in plan.work_aliases},
                    {"source_native_id": f"F04_JINR_ARTICLES_RU:{ITEM_UUID}", "doi": "10.1000/article"},
                )
                self.assertFalse(any(alias.startswith("edn:") for alias in work["work_aliases"]))
                self.assertEqual(len(edn_warnings), 1)
                self.assertIn(ITEM_UUID, edn_warnings[0])

                for value in values:
                    self.assertIn(value.upper(), edn_warnings[0])

                self.assertEqual(self._file_bytes(), before)

    def test_missing_explicit_genre_or_journal_is_rejected_without_writes(self) -> None:
        """Подготовленный пакет требует явных genre и journal_id до создания плана."""

        for field, value in (("genre", None), ("genre", "preprint"), ("journal_id", None), ("journal_id", " ")):
            with self.subTest(field=field, value=value):
                original = self.fixture.entry[field]
                self.fixture.entry[field] = value
                self.fixture.save_batch()
                before = self._file_bytes()

                with self.assertRaisesRegex(ValueError, field):
                    self._plan()

                self.assertEqual(self._file_bytes(), before)
                self.fixture.entry[field] = original

    def test_incompatible_options_are_rejected(self) -> None:
        """План журнального PDF не принимает другой способ, объём или контекст получения."""

        for field, value in (
            ("content_role", "metadata_only"), ("acquisition_method", "crawler"),
            ("acquisition_scope", "sample"), ("response_representation", "json"),
            ("request_context_type", "source"),
        ):
            with self.subTest(field=field):
                options = replace(self.options, **{field: value})

                with self.assertRaisesRegex(ValueError, field):
                    plan_article_batch(
                        self.root, self.fixture.path, options,
                        metadata_rights_record_ids=METADATA_RIGHTS,
                    )

    def test_missing_or_invalid_rights_ids_are_rejected(self) -> None:
        """Пустые и неоднозначные ссылки на права нельзя подставлять автоматически."""

        with self.assertRaisesRegex(ValueError, "rights_record_ids"):
            replace(self.options, rights_record_ids=())

        for rights in ((), ("",), (" right-api",)):
            with self.subTest(rights=rights), self.assertRaisesRegex(ValueError, "прав"):
                plan_article_batch(
                    self.root, self.fixture.path, self.options,
                    metadata_rights_record_ids=rights,
                )

    def test_invalid_edn_is_rejected_even_with_valid_values(self) -> None:
        """Неверный формат EDN остаётся ошибкой и рядом с одним или несколькими верными кодами."""

        for values in (("ABC123",), ("lfzhvr", "ABC123"), ("lfzhvr", "iewszd", "ABC123")):
            with self.subTest(values=values):
                self.fixture.item["metadata"]["local.EDN"] = [{"value": value} for value in values]
                self.fixture.save_metadata()
                before = self._file_bytes()

                with self.assertRaisesRegex(ValueError, "EDN"):
                    self._plan()

                self.assertEqual(self._file_bytes(), before)

    def test_plan_is_offline_read_only_and_schema_valid(self) -> None:
        """План не меняет входные файлы, не обращается в сеть и проходит действующие схемы."""

        before = self._file_bytes()

        with patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена в офлайн-тесте")):
            plan = self._plan()

        self.assertEqual(self._file_bytes(), before)
        schemas = SchemaCatalog(ROOT / "manifests/schemas")

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in getattr(plan, kind):
                with self.subTest(kind=kind):
                    schemas.validate(kind, record)
                    self.assertEqual(record["created_at"], COLLECTED_AT)

    def test_shared_api_response_creates_one_metadata_event(self) -> None:
        """Несколько карточек одной страницы API ссылаются на одно событие получения."""

        batch = self._verified_pair()

        with patch("src.corpus.jinr_article_registration.read_article_batch", return_value=batch):
            plan = self._plan()

        self.assertEqual(len(plan.works), 2)
        metadata_events = [event for event in plan.retrieval_events if event["acquisition_method"] == "api"]
        self.assertEqual(len(metadata_events), 1)
        self.assertEqual(
            {alias["source_retrieval_id"] for alias in plan.work_aliases},
            {metadata_events[0]["retrieval_id"]},
        )

    def test_different_articles_cannot_share_work_id(self) -> None:
        """Один DOI у разных карточек не должен молча перезаписывать работу в пакете."""

        batch = self._verified_pair(same_doi=True)
        before = self._file_bytes()

        with (
            patch("src.corpus.jinr_article_registration.read_article_batch", return_value=batch),
            self.assertRaisesRegex(ValueError, "work_id"),
        ):
            self._plan()

        self.assertEqual(self._file_bytes(), before)

    def test_preflight_requires_existing_rights_before_any_write(self) -> None:
        """Одни ID не заменяют проверенные разрешения в реестре прав."""

        store = self._store()
        plan = self._plan()
        before = self._file_bytes()

        with self.assertRaises(ManifestError):
            store.preflight([plan])

        self.assertEqual(self._file_bytes(), before)
        self.assertEqual(store.records("rights"), [])
        self.assertEqual(store.records("works"), [])
        self.assertEqual(store.records("artifacts"), [])

    def test_manifest_commit_is_idempotent_with_valid_test_rights(self) -> None:
        """Повтор с другим порядком прав и новой датой не меняет ни один реестр."""

        store = self._store()
        plan = self._plan_with_test_rights()
        input_bytes = self._file_bytes()
        preview = store.preflight([copy.deepcopy(plan)])

        self.assertTrue(preview.dry_run)
        self.assertEqual(self._file_bytes(), input_bytes)
        result = store.commit(plan)
        committed_bytes = self._file_bytes()
        reversed_options = replace(self.options, rights_record_ids=tuple(reversed(self.options.rights_record_ids)))
        repeated_candidate = plan_article_batch(
            self.root, self.fixture.path, reversed_options,
            collected_at=LATER_COLLECTED_AT,
            metadata_rights_record_ids=tuple(reversed(METADATA_RIGHTS)),
        )
        repeated, expected_hashes = reconcile_article_plan(store, repeated_candidate)
        store.preflight([repeated], expected_snapshot_hashes=expected_hashes)
        repeated_result = store.commit(repeated, expected_snapshot_hashes=expected_hashes)

        self.assertEqual(result.written_blobs, 0)
        self.assertEqual(sum(repeated_result.inserted.values()), 0)
        self.assertEqual(sum(repeated_result.updated.values()), 0)
        self.assertEqual(self._file_bytes(), committed_bytes)
        self.assertEqual(len(store.records("works")), 1)
        self.assertEqual(len(store.records("artifacts")), 1)
        self.assertEqual(len(store.records("retrieval_events")), 3)
        self.assertEqual(len(store.records("work_aliases")), 3)
        self.assertNotIn("corpus_group_id", store.records("works")[0])

        for path, body in input_bytes.items():
            self.assertEqual((self.root / path).read_bytes(), body)

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in store.records(kind):
                self.assertEqual(record["created_at"], COLLECTED_AT)

        for alias in store.records("work_aliases"):
            self.assertEqual(alias["verified_at"], COLLECTED_AT)

        self.assertTrue(store.audit().ok)

    def test_reimport_preserves_reverse_retrieval_order_without_revisions(self) -> None:
        """Повтор двух получений тех же байтов сохраняет исходный порядок и всю историю."""

        store = self._store()
        plan = self._plan_with_test_rights()
        initial_retrievals = plan.artifacts[0]["retrievals"]
        initial_retrievals.sort(key=lambda record: record["retrieval_id"], reverse=True)

        self.assertEqual(len(initial_retrievals), 2)
        self.assertGreater(initial_retrievals[0]["retrieval_id"], initial_retrievals[1]["retrieval_id"])

        initial_plan, initial_hashes = reconcile_article_plan(store, plan)
        store.commit(initial_plan, expected_snapshot_hashes=initial_hashes)
        committed_bytes = self._file_bytes()
        committed_artifact = store.records("artifacts")[0]
        committed_revisions = store.records("artifact_revisions")
        committed_decisions = store.records("operation_decisions")

        self.assertEqual(committed_artifact["retrievals"], initial_retrievals)

        repeated_candidate = self._plan(LATER_COLLECTED_AT)
        repeated_candidate.artifacts[0]["retrievals"].sort(key=lambda record: record["retrieval_id"])
        repeated, expected_hashes = reconcile_article_plan(store, repeated_candidate)
        preview = store.preflight([repeated], expected_snapshot_hashes=expected_hashes)
        repeated_result = store.commit(repeated, expected_snapshot_hashes=expected_hashes)

        self.assertEqual(repeated_result.updated["artifacts"], 0)
        self.assertEqual(sum(preview.inserted.values()), 0)
        self.assertEqual(sum(preview.updated.values()), 0)
        self.assertEqual(sum(repeated_result.inserted.values()), 0)
        self.assertEqual(sum(repeated_result.updated.values()), 0)
        self.assertEqual(repeated_result.written_blobs, 0)
        self.assertEqual(self._file_bytes(), committed_bytes)
        self.assertEqual(store.records("artifacts"), [committed_artifact])
        self.assertEqual(store.records("artifact_revisions"), committed_revisions)
        self.assertEqual(store.records("operation_decisions"), committed_decisions)
        self.assertTrue(store.audit().ok)

    def test_existing_work_conflict_stops_reconciliation_without_writes(self) -> None:
        """Изменение подтверждённого года останавливает регистрацию до изменения реестров."""

        store = self._store()
        store.commit(self._plan_with_test_rights())
        self.fixture.item["metadata"]["dc.date.issued"] = [{"value": "2025"}]
        self.fixture.save_metadata()
        candidate = self._plan(LATER_COLLECTED_AT)
        before = self._file_bytes()

        with self.assertRaisesRegex(ManifestConflictError, "конфликт"):
            reconcile_article_plan(store, candidate)

        self.assertEqual(self._file_bytes(), before)
        self.assertEqual(store.records("works")[0]["published_year"], 2024)
        self.assertEqual(store.records("works")[0]["eligibility_status"], "pending")
        self.assertEqual(store.records("identity_conflicts"), [])


if __name__ == "__main__":
    unittest.main()
